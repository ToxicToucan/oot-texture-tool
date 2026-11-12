// SPDX-License-Identifier: GPL-3.0-or-later
// texconv: the texture tool's codec helper (texture_tool.py drives it). Linear (unswizzled) blocks and pixels only.
//   texconv decode-astc <in.blocks> <out.rgba> W H BW BH srgb|linear   ASTC blocks -> RGBA8 (W*H*4 bytes)
//   texconv encode-bc7  <in.rgba>   <out.blocks> W H [quality 0..6]    RGBA8 -> BC7 blocks (sRGB-encoded values stay as they are)
//   texconv encode-bc1|bc3|bc4|bc5 <in.rgba> <out.blocks> W H [level 0..18]   RGBA8 (sides multiples of 4) -> BC1/BC3/BC4 (R)/BC5 (R,G)
// Uses the astc2bc7 library (textures/: ARM astcenc and Binomial bc7e, Apache-2.0) and rgbcx from bc7enc_rdo
// (Richard Geldreich, Jr., MIT or public domain) for BC1 to BC5.
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#include "astc2bc7.h"
#include "rgbcx.h"

static std::vector<uint8_t> read_all(const char* p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}
static bool write_all(const char* p, const void* d, size_t n) {
    std::ofstream f(p, std::ios::binary);
    f.write(static_cast<const char*>(d), static_cast<std::streamsize>(n));
    return static_cast<bool>(f);
}

static uint16_t to565(const uint8_t* c) {
    return static_cast<uint16_t>(((c[0] * 31 + 127) / 255) << 11 | ((c[1] * 63 + 127) / 255) << 5 | ((c[2] * 31 + 127) / 255));
}
static void from565(uint16_t v, int* c) {
    const int r = v >> 11, g = (v >> 5) & 63, b = v & 31;
    c[0] = (r << 3) | (r >> 2); c[1] = (g << 2) | (g >> 4); c[2] = (b << 3) | (b >> 2);
}

// BC1 with cut-out alpha: blocks with any pixel under alpha 128 use the 3-colour mode (colour0 <= colour1), where index 3 is
// transparent black; the others go to rgbcx (which ignores alpha). Endpoints for a cut-out block: its darkest and brightest
// opaque pixels.
static void encode_bc1_alpha(uint32_t level, uint8_t* dst, const uint8_t* px) {
    int lo = -1, hi = -1, lo_l = 1 << 30, hi_l = -1;
    bool cut = false;
    for (int i = 0; i < 16; ++i) {
        const uint8_t* p = px + i * 4;
        if (p[3] < 128) { cut = true; continue; }
        const int l = p[0] * 2 + p[1] * 5 + p[2];
        if (l < lo_l) { lo_l = l; lo = i; }
        if (l > hi_l) { hi_l = l; hi = i; }
    }
    if (!cut) { rgbcx::encode_bc1(level, dst, px, true, false); return; }
    uint16_t c0 = lo < 0 ? 0 : to565(px + lo * 4), c1 = hi < 0 ? 0 : to565(px + hi * 4);
    if (c0 > c1) std::swap(c0, c1);
    int pal[3][3];
    from565(c0, pal[0]); from565(c1, pal[1]);
    for (int c = 0; c < 3; ++c) pal[2][c] = (pal[0][c] + pal[1][c]) / 2;
    uint32_t sel = 0;
    for (int i = 0; i < 16; ++i) {
        const uint8_t* p = px + i * 4;
        uint32_t s = 3;
        if (p[3] >= 128) {
            int best = 1 << 30;
            for (int j = 0; j < 3; ++j) {
                int d = 0;
                for (int c = 0; c < 3; ++c) d += (p[c] - pal[j][c]) * (p[c] - pal[j][c]);
                if (d < best) { best = d; s = j; }
            }
        }
        sel |= s << (2 * i);
    }
    dst[0] = c0 & 0xff; dst[1] = c0 >> 8; dst[2] = c1 & 0xff; dst[3] = c1 >> 8;
    std::memcpy(dst + 4, &sel, 4);
}

int main(int argc, char** argv) {
    if (argc < 6) {
        std::fprintf(stderr, "usage: texconv decode-astc <in> <out> W H BW BH srgb|linear | encode-bc7 <in> <out> W H [quality]\n"
                             "       texconv encode-bc1|encode-bc3|encode-bc4|encode-bc5 <in> <out> W H [level 0..18]\n");
        return 2;
    }
    const std::string cmd = argv[1];
    const uint32_t w = std::strtoul(argv[4], nullptr, 10), h = std::strtoul(argv[5], nullptr, 10);
    const std::vector<uint8_t> in = read_all(argv[2]);
    if (cmd == "decode-astc") {
        if (argc < 9) return 2;
        a2b_input ai{std::strcmp(argv[8], "srgb") == 0 ? A2B_ASTC_SRGB : A2B_ASTC_UNORM, w, h,
                     static_cast<uint32_t>(std::atoi(argv[6])), static_cast<uint32_t>(std::atoi(argv[7]))};
        std::vector<uint8_t> rgba(static_cast<size_t>(w) * h * 4);
        const a2b_status st = a2b_decode_astc_rgba8(&ai, in.data(), in.size(), 0, rgba.data(), nullptr);
        if (st != A2B_OK) { std::fprintf(stderr, "decode: %s\n", a2b_status_string(st)); return 1; }
        return write_all(argv[3], rgba.data(), rgba.size()) ? 0 : 1;
    }
    if (cmd == "encode-bc7") {
        if (in.size() != static_cast<size_t>(w) * h * 4) { std::fprintf(stderr, "expected %u x %u RGBA8\n", w, h); return 1; }
        a2b_options opt{argc > 6 ? std::atoi(argv[6]) : 4, 0, 0, 0};
        std::vector<uint8_t> out(static_cast<size_t>((w + 3) / 4) * ((h + 3) / 4) * 16);
        const a2b_status st = a2b_encode_bc7(w, h, in.data(), &opt, out.data(), out.size(), nullptr);
        if (st != A2B_OK) { std::fprintf(stderr, "encode: %s\n", a2b_status_string(st)); return 1; }
        return write_all(argv[3], out.data(), out.size()) ? 0 : 1;
    }
    if (cmd == "encode-bc1" || cmd == "encode-bc3" || cmd == "encode-bc4" || cmd == "encode-bc5") {
        if (w % 4 || h % 4 || in.size() != static_cast<size_t>(w) * h * 4) {
            std::fprintf(stderr, "expected %u x %u RGBA8 with both sides a multiple of 4\n", w, h);
            return 1;
        }
        const int kind = cmd[9] - '0';
        const uint32_t level = argc > 6 ? std::min<uint32_t>(std::strtoul(argv[6], nullptr, 10), rgbcx::MAX_LEVEL) : 10;
        const size_t bpb = (kind == 1 || kind == 4) ? 8 : 16;
        rgbcx::init();
        std::vector<uint8_t> out(static_cast<size_t>(w / 4) * (h / 4) * bpb);
        uint8_t px[64];
        size_t k = 0;
        for (uint32_t by = 0; by < h; by += 4)
            for (uint32_t bx = 0; bx < w; bx += 4, ++k) {
                for (int y = 0; y < 4; ++y) std::memcpy(px + y * 16, &in[((by + y) * static_cast<size_t>(w) + bx) * 4], 16);
                uint8_t* dst = &out[k * bpb];
                if (kind == 1) encode_bc1_alpha(level, dst, px);
                else if (kind == 3) rgbcx::encode_bc3(level, dst, px);
                else if (kind == 4) rgbcx::encode_bc4(dst, px, 4);
                else rgbcx::encode_bc5(dst, px, 0, 1, 4);
            }
        return write_all(argv[3], out.data(), out.size()) ? 0 : 1;
    }
    std::fprintf(stderr, "unknown command %s\n", cmd.c_str());
    return 2;
}
