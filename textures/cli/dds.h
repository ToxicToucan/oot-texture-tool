// Minimal DDS reader/writer for BC1..BC7, mip 0 only. GPL-3.0-or-later.
#pragma once
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "astc2bc7.h"

namespace dds_detail {
struct Map { uint32_t dxgi; a2b_format fmt; };
inline const Map kMap[] = {
    {71, A2B_BC1_UNORM}, {72, A2B_BC1_SRGB}, {74, A2B_BC2_UNORM}, {75, A2B_BC2_SRGB}, {77, A2B_BC3_UNORM},
    {78, A2B_BC3_SRGB},  {80, A2B_BC4_UNORM}, {81, A2B_BC4_SNORM}, {83, A2B_BC5_UNORM}, {84, A2B_BC5_SNORM},
    {95, A2B_BC6H_UF16}, {96, A2B_BC6H_SF16}, {98, A2B_BC7_UNORM}, {99, A2B_BC7_SRGB},
};
inline uint32_t rd(const std::vector<uint8_t>& f, size_t o) {
    uint32_t v;
    memcpy(&v, f.data() + o, 4);
    return v;
}
}  // namespace dds_detail

// Fills in/data/data_size (data points into file). Handles DX10 headers and the legacy DXT1/3/5, ATI1/2, BC4U/BC5U FourCCs.
inline bool dds_read(const std::vector<uint8_t>& f, a2b_input& in, const uint8_t*& data, size_t& data_size, std::string& err) {
    using namespace dds_detail;
    if (f.size() < 128 || rd(f, 0) != 0x20534444 || rd(f, 4) != 124) { err = "not a DDS file"; return false; }
    uint32_t h = rd(f, 12), w = rd(f, 16), pf_flags = rd(f, 80), fourcc = rd(f, 84);
    size_t off = 128;
    a2b_format fmt = (a2b_format)0;
    if (!(pf_flags & 4)) { err = "uncompressed DDS is not a BCn texture"; return false; }
    if (fourcc == 0x30315844) {  // DX10
        if (f.size() < 148) { err = "truncated DX10 header"; return false; }
        uint32_t dxgi = rd(f, 128);
        for (auto& m : kMap) if (m.dxgi == dxgi) fmt = m.fmt;
        if (rd(f, 136) > 1 && rd(f, 140) > 1) { err = "texture arrays are not supported"; return false; }
        off = 148;
    } else {
        switch (fourcc) {
            case 0x31545844: fmt = A2B_BC1_UNORM; break;                       // DXT1
            case 0x33545844: fmt = A2B_BC2_UNORM; break;                       // DXT3
            case 0x35545844: fmt = A2B_BC3_UNORM; break;                       // DXT5
            case 0x31495441: case 0x55344342: fmt = A2B_BC4_UNORM; break;      // ATI1, BC4U
            case 0x32495441: case 0x55354342: fmt = A2B_BC5_UNORM; break;      // ATI2, BC5U
        }
    }
    if (!fmt) { err = "unsupported DDS pixel format"; return false; }
    in = {fmt, w, h, 0, 0};
    uint64_t need = a2b_output_size(&in);
    if (f.size() < off + need) { err = "DDS data truncated"; return false; }
    data = f.data() + off;
    data_size = (size_t)need;  // mip 0 only
    return true;
}

inline std::vector<uint8_t> dds_header(a2b_format fmt, uint32_t w, uint32_t h, uint32_t linear_size) {
    using namespace dds_detail;
    uint32_t dxgi = 0;
    for (auto& m : kMap) if (m.fmt == fmt) dxgi = m.dxgi;
    std::vector<uint8_t> out(148, 0);
    auto put = [&](size_t o, uint32_t v) { memcpy(out.data() + o, &v, 4); };
    put(0, 0x20534444);
    put(4, 124);
    put(8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000);  // CAPS HEIGHT WIDTH PIXELFORMAT LINEARSIZE
    put(12, h);
    put(16, w);
    put(20, linear_size);
    put(28, 1);                 // mip count
    put(76, 32);                // DDS_PIXELFORMAT size
    put(80, 4);                 // DDPF_FOURCC
    put(84, 0x30315844);        // 'DX10'
    put(108, 0x1000);           // DDSCAPS_TEXTURE
    put(128, dxgi);
    put(132, 3);                // TEXTURE2D
    put(140, 1);                // array size
    return out;
}
