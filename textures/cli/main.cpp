// astc2bc7 command line. GPL-3.0-or-later.
//
//   astc2bc7 <in.astc|in.dds> <out.dds> [--srgb|--linear] [--quality 0..6] [--threads N] [--perceptual] [--allow-opaque-mode6 | --no-opaque-mode6]
//            [--dll-dir DIR] [--dump-rgba FILE] [--bench N] [--raw WxH:BWxBH] [--raw-out]
//
//   .astc   astcenc's file format (16-byte header, LDR only); sRGB is not stored in it, so say --srgb (default linear)
//   .dds    BC1..BC7 with a DX10 or legacy FourCC header (mip 0 only): copied through untouched
//   --raw WxH:BWxBH  the input is bare ASTC blocks of that footprint (any file extension)
//   --raw-out        write the bare BC7 blocks instead of a .dds
//   --dump-rgba F    also write the intermediate ASTC decode as u32 w, u32 h, u32 srgb, then RGBA8 (for the oracle)
//   --bench N        repeat the transcode N times in memory, print MPix/s (decode, encode, total)
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "astc2bc7.h"
#include "dds.h"

static bool read_file(const std::string& p, std::vector<uint8_t>& out) {
    FILE* f = fopen(p.c_str(), "rb");
    if (!f) return false;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    out.resize((size_t)n);
    bool ok = n == 0 || fread(out.data(), 1, (size_t)n, f) == (size_t)n;
    fclose(f);
    return ok;
}

static bool write_file(const std::string& p, const void* d1, size_t n1, const void* d2 = nullptr, size_t n2 = 0) {
    FILE* f = fopen(p.c_str(), "wb");
    if (!f) return false;
    bool ok = fwrite(d1, 1, n1, f) == n1 && (!n2 || fwrite(d2, 1, n2, f) == n2);
    return (fclose(f) == 0) && ok;
}

static bool ends_with(const std::string& s, const char* suf) {
    size_t n = strlen(suf);
    if (s.size() < n) return false;
    for (size_t i = 0; i < n; i++)
        if (tolower((unsigned char)s[s.size() - n + i]) != suf[i]) return false;
    return true;
}

static void usage() {
    fprintf(stderr,
            "usage: astc2bc7 <in.astc|in.dds> <out.dds> [--srgb|--linear] [--quality 0..6] [--threads N] [--perceptual]\n"
            "                [--dll-dir DIR] [--dump-rgba FILE] [--bench N] [--raw WxH:BWxBH] [--raw-out]\n");
}

int main(int argc, char** argv) {
    std::string in_path, out_path, dll_dir, dump_path, raw_spec;
    bool srgb = false, raw_out = false;
    int bench = 0;
    a2b_options opt{-1, 0, 0, 0};
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> const char* { return i + 1 < argc ? argv[++i] : (usage(), exit(2), nullptr); };
        if (a == "--srgb") srgb = true;
        else if (a == "--linear") srgb = false;
        else if (a == "--quality") opt.quality = atoi(next());
        else if (a == "--threads") opt.threads = atoi(next());
        else if (a == "--perceptual") opt.perceptual = 1;
        else if (a == "--allow-opaque-mode6") opt.allow_opaque_mode6 = 1;      // raw bc7e: alpha may drift
        else if (a == "--no-opaque-mode6") opt.allow_opaque_mode6 = 2;       // legacy: mode 6 off for opaque blocks
        else if (a == "--dll-dir") dll_dir = next();
        else if (a == "--dump-rgba") dump_path = next();
        else if (a == "--bench") bench = atoi(next());
        else if (a == "--raw") raw_spec = next();
        else if (a == "--raw-out") raw_out = true;
        else if (a.size() > 1 && a[0] == '-') { usage(); return 2; }
        else if (in_path.empty()) in_path = a;
        else if (out_path.empty()) out_path = a;
        else { usage(); return 2; }
    }
    if (in_path.empty() || out_path.empty()) { usage(); return 2; }

    std::vector<uint8_t> file;
    if (!read_file(in_path, file)) { fprintf(stderr, "cannot read %s\n", in_path.c_str()); return 1; }

    a2b_input in{};
    const uint8_t* data = nullptr;
    size_t data_size = 0;
    if (!raw_spec.empty()) {
        unsigned w, h, bw, bh;
        if (sscanf(raw_spec.c_str(), "%ux%u:%ux%u", &w, &h, &bw, &bh) != 4) { usage(); return 2; }
        in = {srgb ? A2B_ASTC_SRGB : A2B_ASTC_UNORM, w, h, bw, bh};
        data = file.data();
        data_size = file.size();
    } else if (ends_with(in_path, ".astc")) {
        if (file.size() < 16 || file[0] != 0x13 || file[1] != 0xAB || file[2] != 0xA1 || file[3] != 0x5C) {
            fprintf(stderr, "not an astcenc .astc file\n");
            return 1;
        }
        if (file[6] != 1) { fprintf(stderr, "3D ASTC blocks are not supported\n"); return 1; }
        auto u24 = [&](int o) { return (uint32_t)file[o] | (uint32_t)file[o + 1] << 8 | (uint32_t)file[o + 2] << 16; };
        if (u24(13) != 1) { fprintf(stderr, "3D ASTC images are not supported\n"); return 1; }
        in = {srgb ? A2B_ASTC_SRGB : A2B_ASTC_UNORM, u24(7), u24(10), file[4], file[5]};
        data = file.data() + 16;
        data_size = file.size() - 16;
    } else if (ends_with(in_path, ".dds")) {
        std::string err;
        if (!dds_read(file, in, data, data_size, err)) { fprintf(stderr, "%s: %s\n", in_path.c_str(), err.c_str()); return 1; }
    } else {
        fprintf(stderr, "unknown input type (use .astc, .dds or --raw)\n");
        return 2;
    }

    a2b_status st = a2b_init(dll_dir.empty() ? nullptr : dll_dir.c_str());
    if (st != A2B_OK && a2b_is_astc(in.format)) { fprintf(stderr, "init: %s\n", a2b_status_string(st)); return 1; }

    std::vector<uint8_t> out((size_t)a2b_output_size(&in));
    a2b_format out_format = in.format;
    a2b_stats stats{};
    if (!dump_path.empty() && a2b_is_astc(in.format)) {
        std::vector<uint8_t> rgba((size_t)in.width * in.height * 4);
        st = a2b_decode_astc_rgba8(&in, data, data_size, opt.threads, rgba.data(), nullptr);
        if (st != A2B_OK) { fprintf(stderr, "decode: %s\n", a2b_status_string(st)); return 1; }
        uint32_t hdr[3] = {in.width, in.height, a2b_is_srgb(in.format) ? 1u : 0u};
        if (!write_file(dump_path, hdr, sizeof hdr, rgba.data(), rgba.size())) { fprintf(stderr, "cannot write %s\n", dump_path.c_str()); return 1; }
    }

    double dec = 0, enc = 0, tot = 0;
    const int reps = std::max(1, bench);
    for (int r = 0; r < reps; r++) {
        auto t0 = std::chrono::steady_clock::now();
        st = a2b_transcode(&in, data, data_size, &opt, &out_format, out.data(), out.size(), &stats);
        tot += std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        dec += stats.decode_seconds;
        enc += stats.encode_seconds;
        if (st != A2B_OK) { fprintf(stderr, "transcode: %s\n", a2b_status_string(st)); return 1; }
    }
    if (bench) {
        const double mpix = (double)in.width * in.height * reps / 1e6;
        printf("bench: %ux%u x%d  decode %.1f MPix/s  encode %.1f MPix/s  total %.1f MPix/s  (isa %s, quality %d, threads %d)\n",
               in.width, in.height, reps, mpix / dec, mpix / enc, mpix / tot, stats.astc_isa ? stats.astc_isa : "-",
               opt.quality < 0 ? A2B_DEFAULT_QUALITY : opt.quality, opt.threads);
    }
    bool ok;
    if (raw_out) ok = write_file(out_path, out.data(), out.size());
    else {
        std::vector<uint8_t> hdr = dds_header(out_format, in.width, in.height, (uint32_t)out.size());
        ok = write_file(out_path, hdr.data(), hdr.size(), out.data(), out.size());
    }
    if (!ok) { fprintf(stderr, "cannot write %s\n", out_path.c_str()); return 1; }
    return 0;
}
