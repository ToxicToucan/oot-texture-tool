# textures: ASTC to BC7 transcoder

C++20 library plus CLI (`astc2bc7`) that turns ASTC LDR / sRGB textures (all 14 footprints, 4x4 to 12x12) into BC7, once,
at setup time, so the renderer never keeps 4 to 9 times larger RGBA8 copies. BCn input is copied through untouched.

Pipeline: ASTC to RGBA8 with ARM astcenc 5.7.0, then BC7 with `bc7e` (ISPC, from bc7enc_rdo). Both stages are
multithreaded (decode in bands of block rows, one astcenc context per thread; encode in 64-block chunks).
astcenc is built decode-only as three DLLs (`astcdec-avx2/sse4.1/sse2-shared.dll`) and the one the CPU supports is loaded at run time;
bc7e's ISPC object carries sse4, avx2 and avx512 paths with its own dispatch.

## Behaviour that matters

- sRGB: ASTC sRGB data is decoded with astcenc's sRGB profile (RGB stays sRGB-encoded, alpha linear), BC7 is encoded on those 8-bit values, and the output is tagged `BC7_UNORM_SRGB` (DXGI 99). Linear ASTC gives `BC7_UNORM` (98). The `.astc` file format does not store the profile, so the CLI takes `--srgb`.
- Alpha: opaque source blocks keep alpha exactly 255. Left alone, bc7e stores opaque mode-6 alpha in the shared p-bit (254 half the time) and uses mode 4/5 channel rotation without pinning the swapped-in original alpha (244..253 on normal maps). The first version of this library switched mode 6 and rotation off for opaque blocks, which cost about 2 dB on albedo and 2 to 12 dB on two-channel (RG, B = 0) normal maps, where rotation is what gives the data channel in the alpha slot its own index set. Now (`allow_opaque_mode6 = 0`, the default) opaque blocks are encoded with mode 6 off (pinning it costs more RGB PSNR than the mode gains), with the preset's channel rotations (about +45% encode time at quality 3), and every opaque block is then fixed up exactly: the slot that holds the original alpha gets endpoints that decode to 255 for any index, which touches no data channel. `--allow-opaque-mode6` gives the raw bc7e output (alpha may drift); `--no-opaque-mode6` the first version.
- Partial blocks: image sizes that are not multiples of 4 (or of the ASTC footprint) are handled; edge pixels are replicated into partial BC7 blocks.
- Passthrough: DDS files with BC1 to BC7 (DX10 or legacy FourCC headers) are copied byte for byte, same format.
- Presets: `--quality 0..6` = bc7e ultrafast, veryfast, fast, basic, slow, veryslow, slowest; default 5 (veryslow). Chosen from the sweep below: 5 has the best mean PSNR per unit time; 6 is 2x slower for no mean gain.
- Not handled: ASTC HDR, 3D ASTC, texture arrays and mip chains inside one call (call once per level), non-Windows hosts (`LoadLibrary`).

## Build and use

```powershell
powershell -File textures\scripts\fetch_third_party.ps1     # astc-encoder 5.7.0, bc7enc_rdo, ISPC 1.32.0 into third_party\
pwsh -File textures\scripts\build.ps1                       # CMake + Ninja + clang-cl -> build\release\astc2bc7.exe + 3 DLLs
build\release\astc2bc7.exe in.astc out.dds --srgb [--quality 5] [--threads N] [--dump-rgba f.rgba] [--bench N]
build\release\astc2bc7.exe blocks.bin out.dds --raw 1024x1024:6x6 [--raw-out]     # bare ASTC blocks
```

Library API: `include/astc2bc7.h` (`a2b_transcode`, `a2b_decode_astc_rgba8`, `a2b_encode_bc7`, `a2b_output_size`, C linkage).

## Stand-in corpus and oracle

Corpus (not committed, under `work\corpus\`): 16 CC0 1K images from ambientCG (albedo, normal, gray, alpha, high-frequency noise;
`textures_src\MANIFEST.tsv`), encoded with astcenc 5.7.0 `-medium` by `scripts/make_astc_corpus.py` at all 14 block sizes, plus
odd-size crops (1000x750, 333x257, 31x17) at 4x4, 6x6, 8x8 and 12x12: 260 `.astc` files, with astcenc's own decode as ground truth (`ref_png`).

```powershell
pwsh -File textures\oracle\build.ps1                          # bc7dec.exe, the independent BC7 decoder (bc7enc_rdo's reference decoder)
python textures\oracle\run_oracle.py --out work\corpus\textures_oracle_run [--sweep]
```

Checks per file: (1) our ASTC decode equals astcenc's CLI output byte for byte; (2) PSNR and SSIM of the BC7 result (decoded by `bc7dec`, not by our code) against the ASTC decode;
(3) DDS format tag, size and payload length; (4) alpha stays 255 on opaque sources. Plus BCn passthrough (BC1, BC3, BC4, BC5, BC7, odd sizes) bit identical, and the corpus mean.

**Thresholds and why**: PSNR RGB and RGBA at least 33 dB per file, SSIM (luma) at least 0.97 per file, mean PSNR RGB at least 48 dB.
They sit 1.6 dB, 0.004 and 3.9 dB under the measured worst case or mean, so a real regression trips them and noise does not. 33 dB is far above what a broken encoder gives (under 25 dB)
and below the roughly 40 dB usually called visually lossless, because the worst files are the noisiest 1024x1024 normal map at 4x4 to 5x5 (34.65 dB) and a 31x17 alpha crop.
The reference is already lossy: BC7 is measured against what the ASTC decoder produces, not against the original.

**Results** (i9-12900K, 16 threads, preset 5, 260 files, 113 s): ORACLE PASS. Decode identity 260 of 260 (0 differing bytes). Alpha 260 of 260. Passthrough 11 of 11.
PSNR RGB min 34.96, mean 53.81 dB (by kind albedo 50.8, normal 62.1, gray 60.2, noise 48.0, alpha 46.7; the first-version converter gave min 34.65, mean 51.95, normal 55.1). SSIM luma min 0.9854, mean 0.997.

**Throughput** (preset 5, 1024x1024 sRGB 6x6, in memory, 16 threads): decode 117 MPix/s, BC7 encode 6.5 MPix/s, total 5.9 MPix/s (0.8 MPix/s on one thread, 7 to 9x scaling on 16 cores).
Cold CLI process including file IO: 3.8 to 4.7 MPix/s. A 600 MPix texture set would take about two minutes.

Preset sweep (56 files, 16 threads; encode MPix/s measured on one 10x10 sRGB file):

| preset | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| mean PSNR RGB (dB) | 46.82 | 49.77 | 49.91 | 50.72 | 50.59 | 51.68 | 51.65 |
| min PSNR RGB (dB) | 28.88 | 33.47 | 32.65 | 33.70 | 34.08 | 34.65 | 34.94 |
| encode MPix/s | 395 | 67 | 46 | 31 | 26 | 5.8 | 3.0 |

SSIM is computed on luma of the RGB channels, also for normal maps, where it is only a rough guide.

## Provenance and licences

| Component | Source | Licence |
|---|---|---|
| astcenc 5.7.0 (decoder DLLs, header) | github.com/ARM-software/astc-encoder, tag 5.7.0 | Apache-2.0 (`licenses/LICENSE.astc-encoder.Apache-2.0`) |
| `bc7e.ispc` | github.com/richgel999/bc7enc_rdo `b9438627`, Binomial LLC | Apache-2.0 (`licenses/LICENSE.bc7enc_rdo`) |
| `bc7decomp_ref.cpp` (oracle's `bc7dec` only) | same repo | MIT or public domain, same LICENSE |
| ISPC 1.32.0 (build tool, not shipped) | github.com/ispc/ispc | BSD-3-Clause |
| everything else here | written for this project | GPL-3.0-or-later |

All are GPL-3.0 compatible (Apache-2.0 is one-way compatible with GPLv3). Pinned versions and hashes: `THIRD_PARTY.md`.
