// SPDX-License-Identifier: GPL-3.0-or-later
// texconv: the texture tool's codec helper (texture_tool.py drives it). Linear (unswizzled) blocks and pixels only.
//   texconv decode-astc <in.blocks> <out.rgba> W H BW BH srgb|linear   ASTC blocks -> RGBA8 (W*H*4 bytes)
//   texconv encode-bc7  <in.rgba>   <out.blocks> W H [quality 0..6]    RGBA8 -> BC7 blocks (sRGB-encoded values stay as they are)
// Uses the astc2bc7 library (textures/: ARM astcenc and Binomial bc7e, Apache-2.0).
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#include "astc2bc7.h"

static std::vector<uint8_t> read_all(const char* p) {
    std::ifstream f(p, std::ios::binary);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}
static bool write_all(const char* p, const void* d, size_t n) {
    std::ofstream f(p, std::ios::binary);
    f.write(static_cast<const char*>(d), static_cast<std::streamsize>(n));
    return static_cast<bool>(f);
}

int main(int argc, char** argv) {
    if (argc < 7) {
        std::fprintf(stderr, "usage: texconv decode-astc <in> <out> W H BW BH srgb|linear | encode-bc7 <in> <out> W H [quality]\n");
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
    std::fprintf(stderr, "unknown command %s\n", cmd.c_str());
    return 2;
}
