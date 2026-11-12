// bc7dec - independent BC7 decoder for the ASTC->BC7 oracle.
//
// Usage: bc7dec <in.dds> <out.rgba>
//
// Reads a DDS file with a DX10 extension header (DXGI_FORMAT_BC7_UNORM = 98 or
// DXGI_FORMAT_BC7_UNORM_SRGB = 99), decodes mip 0 (array slice 0) to RGBA8 with the
// bc7decomp reference decoder (third_party/bc7enc_rdo/bc7decomp_ref.cpp), cropped to
// width x height, and writes:
//   u32 width   (little endian)
//   u32 height  (little endian)
//   u32 srgb    (0 = BC7_UNORM, 1 = BC7_UNORM_SRGB; bytes are identical, only the flag differs)
//   width * height * 4 bytes of RGBA8, row-major, top row first, no padding.
//
// Exit codes: 0 ok, 1 decode/parse error (message on stderr, no output file written), 2 usage.

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

// bc7decomp.h declares namespace bc7decomp (color_rgba) and namespace bc7decomp_ref (unpack_bc7).
#include "bc7decomp.h"

namespace {

const uint32_t kDdsMagic = 0x20534444u;     // "DDS " little endian
const uint32_t kHeaderSize = 124;           // DDS_HEADER.dwSize
const uint32_t kPixelFormatSize = 32;       // DDS_PIXELFORMAT.dwSize
const uint32_t kDdpfFourCC = 0x4;           // DDPF_FOURCC
const uint32_t kFourCCDX10 = 0x30315844u;   // "DX10"
const uint32_t kDxgiBC7Unorm = 98;
const uint32_t kDxgiBC7UnormSrgb = 99;
const uint32_t kDimTexture2D = 3;           // D3D10_RESOURCE_DIMENSION_TEXTURE2D

// File offsets (magic is 4 bytes, then DDS_HEADER, then DDS_HEADER_DXT10).
const size_t kOffHeight = 4 + 8;            // DDS_HEADER.dwHeight
const size_t kOffWidth = 4 + 12;            // DDS_HEADER.dwWidth
const size_t kOffPfSize = 4 + 72;           // DDS_HEADER.ddspf.dwSize
const size_t kOffPfFlags = 4 + 76;          // DDS_HEADER.ddspf.dwFlags
const size_t kOffPfFourCC = 4 + 80;         // DDS_HEADER.ddspf.dwFourCC
const size_t kOffDx10 = 4 + kHeaderSize;    // 128: DDS_HEADER_DXT10
const size_t kOffDxgi = kOffDx10 + 0;
const size_t kOffDim = kOffDx10 + 4;
const size_t kOffArraySize = kOffDx10 + 12;
const size_t kOffData = kOffDx10 + 20;      // 148

uint32_t rd32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

void wr32(uint8_t* p, uint32_t v) {
  p[0] = (uint8_t)v;
  p[1] = (uint8_t)(v >> 8);
  p[2] = (uint8_t)(v >> 16);
  p[3] = (uint8_t)(v >> 24);
}

bool read_file(const char* path, std::vector<uint8_t>& out) {
  FILE* f = std::fopen(path, "rb");
  if (!f) return false;
  std::fseek(f, 0, SEEK_END);
  long len = std::ftell(f);
  std::fseek(f, 0, SEEK_SET);
  if (len < 0) {
    std::fclose(f);
    return false;
  }
  out.resize((size_t)len);
  size_t got = len ? std::fread(out.data(), 1, (size_t)len, f) : 0;
  std::fclose(f);
  return got == (size_t)len;
}

int fail(const char* msg) {
  std::fprintf(stderr, "bc7dec: %s\n", msg);
  return 1;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "usage: bc7dec <in.dds> <out.rgba>\n");
    return 2;
  }

  std::vector<uint8_t> file;
  if (!read_file(argv[1], file)) {
    std::fprintf(stderr, "bc7dec: cannot read %s\n", argv[1]);
    return 1;
  }
  if (file.size() < kOffData) return fail("file too small for DDS + DX10 headers");

  const uint8_t* d = file.data();
  if (rd32(d) != kDdsMagic) return fail("bad DDS magic");
  if (rd32(d + 4) != kHeaderSize) return fail("DDS_HEADER.dwSize != 124");
  if (rd32(d + kOffPfSize) != kPixelFormatSize) return fail("DDS_PIXELFORMAT.dwSize != 32");
  if (!(rd32(d + kOffPfFlags) & kDdpfFourCC)) return fail("pixel format has no FourCC flag");
  if (rd32(d + kOffPfFourCC) != kFourCCDX10) return fail("pixel format FourCC is not DX10");

  const uint32_t width = rd32(d + kOffWidth);
  const uint32_t height = rd32(d + kOffHeight);
  const uint32_t dxgi = rd32(d + kOffDxgi);
  const uint32_t dim = rd32(d + kOffDim);
  if (width == 0 || height == 0) return fail("width or height is zero");
  if (dim != kDimTexture2D) return fail("resourceDimension is not TEXTURE2D");

  uint32_t srgb = 0;
  if (dxgi == kDxgiBC7Unorm) {
    srgb = 0;
  } else if (dxgi == kDxgiBC7UnormSrgb) {
    srgb = 1;
  } else {
    std::fprintf(stderr, "bc7dec: DXGI format %u is not BC7 (98/99)\n", dxgi);
    return 1;
  }
  // arraySize is informational only here: slice 0 is the first data chunk.
  (void)rd32(d + kOffArraySize);

  const uint32_t blocks_x = (width + 3) / 4;
  const uint32_t blocks_y = (height + 3) / 4;
  const size_t need = (size_t)blocks_x * blocks_y * 16;
  if (file.size() - kOffData < need) return fail("file is shorter than mip 0 requires");
  const uint8_t* data = d + kOffData;

  std::vector<uint8_t> out(12 + (size_t)width * height * 4);
  wr32(out.data() + 0, width);
  wr32(out.data() + 4, height);
  wr32(out.data() + 8, srgb);
  uint8_t* pix = out.data() + 12;

  for (uint32_t by = 0; by < blocks_y; by++) {
    for (uint32_t bx = 0; bx < blocks_x; bx++) {
      const uint8_t* bp = data + ((size_t)by * blocks_x + bx) * 16;
      bc7decomp::color_rgba blk[16];
      if (!bc7decomp_ref::unpack_bc7(bp, blk)) {
        std::fprintf(stderr, "bc7dec: invalid BC7 block at (%u,%u), first byte 0x%02X\n", bx, by, bp[0]);
        return 1;
      }
      // blk[] is row-major 4x4 (index = y * 4 + x). Copy only the pixels inside width x height.
      for (uint32_t y = 0; y < 4; y++) {
        uint32_t py = by * 4 + y;
        if (py >= height) break;
        for (uint32_t x = 0; x < 4; x++) {
          uint32_t px = bx * 4 + x;
          if (px >= width) break;
          const uint8_t* src = blk[y * 4 + x].m_comps;
          uint8_t* dst = pix + ((size_t)py * width + px) * 4;
          dst[0] = src[0];
          dst[1] = src[1];
          dst[2] = src[2];
          dst[3] = src[3];
        }
      }
    }
  }

  FILE* o = std::fopen(argv[2], "wb");
  if (!o) {
    std::fprintf(stderr, "bc7dec: cannot open %s for writing\n", argv[2]);
    return 1;
  }
  size_t wrote = std::fwrite(out.data(), 1, out.size(), o);
  std::fclose(o);
  if (wrote != out.size()) return fail("short write");

  std::printf("bc7dec: %ux%u srgb=%u blocks=%ux%u -> %s\n", width, height, srgb, blocks_x, blocks_y, argv[2]);
  return 0;
}
