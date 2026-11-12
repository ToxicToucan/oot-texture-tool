#!/usr/bin/env python3
"""ASTC -> BC7 oracle runner for astc2bc7.exe.

Runs every manifest row through the CLI and checks:
  CHECK 1  decode identity: --dump-rgba pixels equal ref_png (differing byte count must be 0)
  CHECK 2  quality: PSNR RGB / RGBA and SSIM(luma) of the BC7 output (decoded by bc7dec) vs ref_png
  CHECK 3  format: DXGI 99 when srgb=1 else 98; DDS w/h equal manifest; payload = ceil(w/4)*ceil(h/4)*16
  CHECK 4  alpha: has_alpha=0 -> decoded alpha exactly 255 everywhere
  CHECK 5  BCn passthrough (once): hand-built BC1/BC3/BC4/BC5/BC7 DDS in -> payload and DXGI unchanged;
           BC7 DDS fed back in -> bit identical output
  CHECK 6  throughput (once, informational): --bench 5 --threads 16 and --threads 1, plus cold
           end-to-end runs (one CLI process, no --bench, file IO included) on the 1024x1024 main file
  CHECK 7  aggregate: mean PSNR RGB over all rows >= --min-mean-psnr-rgb (only when that flag is given)

--quality is optional. When omitted, --quality is not passed to the CLI (CLI default is used) and the
quality the CLI actually used is parsed from its bench line.

--sweep: quality presets 0..6 on every 4th manifest row that is 1024x1024. No pass/fail.

Usage:
  run_oracle.py --out <dir> [--jobs 4] [--threads-per-job 4] [--quality N]
                [--min-psnr-rgb 33] [--min-psnr-rgba 33] [--min-ssim 0.97] [--min-mean-psnr-rgb 48]
                [--limit N] [--sweep]

Exit 0 when every check passes, 1 otherwise.
"""

import argparse
import concurrent.futures as cf
import csv
import math
import random
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metrics  # noqa: E402  (oracle/metrics.py, not modified)

ROOT = Path(__file__).resolve().parents[3]   # the project root (textures/oracle -> root)
CLI = ROOT / "textures/build/release/astc2bc7.exe"
BC7DEC = ROOT / "textures/build/oracle/bc7dec.exe"
CORPUS = ROOT / "work/corpus/textures_astc"
MANIFEST = CORPUS / "MANIFEST.tsv"
MAIN_BENCH = "Bricks113A_Color__1024x1024__b6x6__srgb.astc"

HDR = 148  # DDS magic + 124-byte header + DX10 extension
CHECKS = (1, 2, 3, 4)
BCN_CASES = [(71, "BC1", 8), (77, "BC3", 16), (80, "BC4", 8), (83, "BC5", 16), (98, "BC7", 16)]
BENCH_RE = re.compile(r"decode ([\d.]+) MPix/s\s+encode ([\d.]+) MPix/s\s+total ([\d.]+) MPix/s")
QUALITY_RE = re.compile(r"quality (\d+)")

COLS = ["astc_file", "kind", "width", "height", "block_w", "block_h", "srgb", "has_alpha",
        "dump_srgb", "dump_diff_bytes", "alpha_not255", "psnr_rgba", "psnr_rgb", "psnr_r", "psnr_g",
        "psnr_b", "psnr_a", "ssim_luma", "dxgi", "payload_bytes", "encode_s",
        "chk1", "chk2", "chk3", "chk4", "result", "reasons"]


# ---------- helpers ----------

def read_manifest():
    rows = []
    with open(MANIFEST, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f, delimiter="\t")):
            r["index"] = i
            r["w"] = int(r["width"])
            r["h"] = int(r["height"])
            r["bw"] = int(r["block_w"])
            r["bh"] = int(r["block_h"])
            r["srgb"] = int(r["srgb"])
            r["has_alpha"] = int(r["has_alpha"])
            r["blocks"] = math.ceil(r["w"] / r["bw"]) * math.ceil(r["h"] / r["bh"])
            rows.append(r)
    return rows


def run_cli(args):
    cmd = [str(CLI)] + [str(a) for a in args]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
    return p.returncode, time.perf_counter() - t0, (p.stdout + p.stderr).strip()


def color_flag(row):
    return "--srgb" if row["srgb"] else "--linear"


def quality_args(quality):
    return [] if quality is None else ["--quality", quality]


def encode_args(row, quality, threads, dds, dump=None):
    a = [CORPUS / row["astc_file"], dds, color_flag(row)] + quality_args(quality) + ["--threads", threads]
    if dump is not None:
        a += ["--dump-rgba", dump]
    return a


def run_bench(row, quality, threads, tmp, n=5):
    rc, _, txt = run_cli([CORPUS / row["astc_file"], tmp / "bench.dds", color_flag(row)]
                         + quality_args(quality) + ["--threads", threads, "--bench", n])
    m = BENCH_RE.search(txt)
    line = next((l for l in txt.splitlines() if l.startswith("bench:")), txt[-200:])
    q = QUALITY_RE.search(line)
    cli_q = int(q.group(1)) if q else None
    if rc != 0 or not m:
        return {"ok": False, "line": line, "quality": cli_q, "decode": None, "encode": None, "total": None}
    return {"ok": True, "line": line, "quality": cli_q, "decode": float(m.group(1)),
            "encode": float(m.group(2)), "total": float(m.group(3))}


def make_dds(path, dxgi, w, h, payload):
    """Hand-built DX10 DDS header (layout checked against CLI output: DXGI at 128, payload at 148)."""
    hdr = bytearray(HDR)
    hdr[0:4] = b"DDS "
    struct.pack_into("<I", hdr, 4, 124)
    struct.pack_into("<I", hdr, 8, 0x81007)          # CAPS|HEIGHT|WIDTH|PIXELFORMAT|LINEARSIZE
    struct.pack_into("<III", hdr, 12, h, w, len(payload))
    struct.pack_into("<I", hdr, 28, 1)               # mip count
    struct.pack_into("<I", hdr, 76, 32)              # DDS_PIXELFORMAT size
    struct.pack_into("<I", hdr, 80, 0x4)             # DDPF_FOURCC
    hdr[84:88] = b"DX10"
    struct.pack_into("<I", hdr, 108, 0x1000)         # DDSCAPS_TEXTURE
    struct.pack_into("<I", hdr, 128, dxgi)
    struct.pack_into("<I", hdr, 132, 3)              # TEXTURE2D
    struct.pack_into("<I", hdr, 140, 1)              # array size
    path.write_bytes(bytes(hdr) + payload)


def stats(vals):
    a = np.asarray(vals, dtype=float)
    return a.min(), a.mean(), np.percentile(a, 5)


# ---------- per-row checks ----------

def process_row(row, idx, args, tmp, dds_dir):
    stem = f"{idx:03d}_" + Path(row["astc_file"]).stem
    dds = dds_dir / f"{Path(row['astc_file']).stem}.dds"
    dump = tmp / f"{stem}.rgba"
    res = {c: "" for c in COLS}
    res.update(astc_file=row["astc_file"], kind=row["kind"], width=row["w"], height=row["h"],
               block_w=row["bw"], block_h=row["bh"], srgb=row["srgb"], has_alpha=row["has_alpha"])
    fails = {}

    def fail(n, msg):
        fails.setdefault(n, msg)

    try:
        rc, secs, txt = run_cli(encode_args(row, args.quality, args.threads_per_job, dds, dump))
        res["encode_s"] = round(secs, 3)
        if rc != 0:
            for n in CHECKS:
                fail(n, f"CLI exit {rc}: {txt[-300:]}")
            raise StopIteration

        ref = metrics.load_rgba_png(CORPUS / row["ref_png"])
        rh, rw = ref.shape[:2]
        if (rw, rh) != (row["w"], row["h"]):
            fail(1, f"ref {rw}x{rh} != manifest {row['w']}x{row['h']}")

        # CHECK 1: decode identity
        raw = dump.read_bytes()
        dw, dh, dsrgb = struct.unpack_from("<III", raw, 0)
        res["dump_srgb"] = dsrgb
        if (dw, dh) != (rw, rh) or len(raw) - 12 != rw * rh * 4:
            res["dump_diff_bytes"] = -1
            fail(1, f"dump {dw}x{dh} vs ref {rw}x{rh}")
        else:
            pix = np.frombuffer(raw, dtype=np.uint8, offset=12).reshape(rh, rw, 4)
            diff = int(np.count_nonzero(pix != ref))
            res["dump_diff_bytes"] = diff
            if diff:
                fail(1, f"{diff} dump bytes differ from ref")

        # CHECK 3: DDS format tag, size, payload
        d = dds.read_bytes()
        if len(d) < HDR or d[:4] != b"DDS " or d[84:88] != b"DX10":
            fail(3, "bad DDS header")
        else:
            hh, ww = struct.unpack_from("<II", d, 12)
            dxgi = struct.unpack_from("<I", d, 128)[0]
            payload = len(d) - HDR
            exp_dxgi = 99 if row["srgb"] else 98
            exp_payload = math.ceil(row["w"] / 4) * math.ceil(row["h"] / 4) * 16
            res["dxgi"] = dxgi
            res["payload_bytes"] = payload
            probs = []
            if dxgi != exp_dxgi:
                probs.append(f"dxgi {dxgi} != {exp_dxgi}")
            if (ww, hh) != (row["w"], row["h"]):
                probs.append(f"dds {ww}x{hh} != {row['w']}x{row['h']}")
            if payload != exp_payload:
                probs.append(f"payload {payload} != {exp_payload}")
            if dsrgb != row["srgb"]:
                probs.append(f"dump srgb {dsrgb} != {row['srgb']}")
            if probs:
                fail(3, "; ".join(probs))

        # CHECK 2 (quality) and CHECK 4 (alpha) on the bc7dec decode
        dec, _ = metrics.decode_dds(dds, BC7DEC)
        alpha_bad = int(np.count_nonzero(dec[..., 3] != 255))
        res["alpha_not255"] = alpha_bad
        if not row["has_alpha"] and alpha_bad:
            fail(4, f"{alpha_bad} decoded alpha values != 255 (has_alpha=0)")
        if dec.shape != ref.shape:
            fail(2, f"decoded {dec.shape[1]}x{dec.shape[0]} != ref {rw}x{rh}")
        else:
            m = metrics.metrics(ref, dec)
            for k, v in m.items():
                res[k] = round(v, 4)
            bad = []
            if m["psnr_rgb"] < args.min_psnr_rgb:
                bad.append(f"psnr_rgb {m['psnr_rgb']:.2f} < {args.min_psnr_rgb}")
            if m["psnr_rgba"] < args.min_psnr_rgba:
                bad.append(f"psnr_rgba {m['psnr_rgba']:.2f} < {args.min_psnr_rgba}")
            if m["ssim_luma"] < args.min_ssim:
                bad.append(f"ssim {m['ssim_luma']:.4f} < {args.min_ssim}")
            if bad:
                fail(2, "; ".join(bad))
    except StopIteration:
        pass
    except Exception as e:  # any unexpected error marks the row failing, never silently passes
        for n in CHECKS:
            fail(n, f"exception: {type(e).__name__}: {e}")

    for n in CHECKS:
        res[f"chk{n}"] = "FAIL" if n in fails else "PASS"
    res["result"] = "FAIL" if fails else "PASS"
    res["reasons"] = " | ".join(f"C{n}: {fails[n]}" for n in sorted(fails))
    return res


def run_rows(rows, args, tmp, dds_dir):
    out = [None] * len(rows)
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(process_row, r, r["index"], args, tmp, dds_dir): i for i, r in enumerate(rows)}
        done = 0
        for f in cf.as_completed(futs):
            out[futs[f]] = f.result()
            done += 1
            if done % 25 == 0 or done == len(rows):
                print(f"  rows {done}/{len(rows)}", flush=True)
    return out


# ---------- CHECK 5: BCn passthrough ----------

def check5(all_rows, args, tmp):
    d = tmp / "bcn"
    d.mkdir(parents=True, exist_ok=True)
    cases = []

    def verify(name, src, dst, rc, txt, whole):
        probs = []
        if rc != 0:
            probs.append(f"CLI exit {rc}: {txt[-200:]}")
        elif not dst.exists():
            probs.append("no output written")
        else:
            a = src.read_bytes()
            b = dst.read_bytes()
            if struct.unpack_from("<I", a, 128)[0] != struct.unpack_from("<I", b, 128)[0]:
                probs.append("DXGI changed")
            if struct.unpack_from("<II", a, 12) != struct.unpack_from("<II", b, 12):
                probs.append("dimensions changed")
            if a[HDR:] != b[HDR:]:
                probs.append("payload differs")
            if whole and a != b:
                probs.append("whole file not bit identical")
        cases.append({"name": name, "ok": not probs, "reason": "; ".join(probs) or "identical"})

    for dxgi, fmt, bsz in BCN_CASES:
        for w, h in ((64, 48), (37, 21)):
            n = math.ceil(w / 4) * math.ceil(h / 4) * bsz
            payload = random.Random(f"{fmt}-{w}x{h}").randbytes(n)
            src = d / f"{fmt}_{w}x{h}.dds"
            dst = d / f"{fmt}_{w}x{h}_out.dds"
            make_dds(src, dxgi, w, h, payload)
            dst.unlink(missing_ok=True)
            rc, _, txt = run_cli([src, dst])
            verify(f"{fmt} {w}x{h} (hand-built)", src, dst, rc, txt, whole=(fmt == "BC7"))

    # BC7 DDS produced by the CLI from a real corpus file, fed back in
    small = min(all_rows, key=lambda r: r["w"] * r["h"])
    rt_src = d / "rt_src.dds"
    rc, _, txt = run_cli(encode_args(small, args.quality, args.threads_per_job, rt_src))
    if rc != 0:
        cases.append({"name": "BC7 round trip encode", "ok": False, "reason": f"CLI exit {rc}: {txt[-200:]}"})
    else:
        rt_out = d / "rt_out.dds"
        rc, _, txt = run_cli([rt_src, rt_out])
        verify(f"BC7 round trip {small['w']}x{small['h']} (CLI-made)", rt_src, rt_out, rc, txt, whole=True)
    return cases


# ---------- CHECK 6: throughput ----------

def cold_runs(row, quality, tmp, threads=16, reps=3):
    """One CLI process per run, no --bench: wall time includes process start, file reads and DDS write."""
    out = []
    for _ in range(reps):
        dst = tmp / "cold.dds"
        dst.unlink(missing_ok=True)
        rc, secs, txt = run_cli([CORPUS / row["astc_file"], dst, color_flag(row)]
                                + quality_args(quality) + ["--threads", threads])
        mpix = row["w"] * row["h"] / 1e6
        out.append({"ok": rc == 0 and dst.exists(), "secs": secs, "mpix_s": mpix / secs if secs > 0 else None,
                    "line": txt[-200:] if rc != 0 else ""})
    return out


def throughput(all_rows, args, tmp):
    main_row = next(r for r in all_rows if r["astc_file"] == MAIN_BENCH)
    big = max(all_rows, key=lambda r: r["blocks"])
    benches = []
    for label, row in (("1024 b6x6 (main)", main_row), (f"largest block count {Path(big['astc_file']).stem}", big)):
        for threads in (16, 1):
            b = run_bench(row, args.quality, threads, tmp)
            benches.append({"label": label, "file": row["astc_file"], "blocks": row["blocks"],
                            "threads": threads, **b})
    cold = {"file": main_row["astc_file"], "runs": cold_runs(main_row, args.quality, tmp)}
    return benches, cold


# ---------- outputs ----------

def write_results(out, results):
    with open(out / "results.tsv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter="\t")
        wr.writerow(COLS)
        for r in results:
            wr.writerow([r[c] for c in COLS])


def write_report(out, args, results, c5, c7, benches, cold, cli_quality, wall, nfail, nrows):
    L = []
    ok_rows = [r for r in results if r["psnr_rgb"] != ""]
    L.append("# ASTC -> BC7 oracle report")
    L.append("")
    L.append(f"- Run: {time.strftime('%Y-%m-%d %H:%M:%S')}, rows {nrows}, "
             f"quality requested {args.quality if args.quality is not None else 'none (CLI default)'}, "
             f"CLI quality used {cli_quality if cli_quality is not None else 'unknown (not in bench line)'}, "
             f"jobs {args.jobs} x threads {args.threads_per_job}")
    thr = (f"Thresholds: psnr_rgb >= {args.min_psnr_rgb}, psnr_rgba >= {args.min_psnr_rgba}, "
           f"ssim >= {args.min_ssim}")
    if args.min_mean_psnr_rgb is not None:
        thr += f", mean psnr_rgb (all rows) >= {args.min_mean_psnr_rgb}"
    L.append(f"- {thr}")
    L.append(f"- Wall time: {wall:.1f} s")
    L.append("")
    L.append("## Pass/fail per check")
    L.append("")
    L.append("| check | pass | fail |")
    L.append("|---|---|---|")
    names = {1: "1 decode identity", 2: "2 quality", 3: "3 format tag/size/payload", 4: "4 alpha"}
    for n in CHECKS:
        p = sum(1 for r in results if r[f"chk{n}"] == "PASS")
        L.append(f"| {names[n]} | {p} | {len(results) - p} |")
    p5 = sum(1 for c in c5 if c["ok"])
    L.append(f"| 5 BCn passthrough (cases) | {p5} | {len(c5) - p5} |")
    if c7 is None:
        L.append("| 7 mean PSNR RGB | not run | not run |")
    else:
        L.append(f"| 7 mean PSNR RGB (all rows) | {1 if c7['ok'] else 0} | {0 if c7['ok'] else 1} |")
    L.append("")
    L.append(f"Rows failing any row check: {sum(1 for r in results if r['result'] == 'FAIL')} of {len(results)}")
    if c7 is not None:
        L.append(f"CHECK 7: mean PSNR RGB over {c7['n']} rows = {c7['mean']:.3f} (threshold {c7['thr']})")
    L.append("")

    if ok_rows:
        L.append("## PSNR and SSIM by block size")
        L.append("")
        L.append("| block | n | PSNR RGB min | mean | p5 | SSIM min | mean | p5 |")
        L.append("|---|---|---|---|---|---|---|---|")
        groups = {}
        for r in ok_rows:
            groups.setdefault(f"{r['block_w']}x{r['block_h']}", []).append(r)
        for k in sorted(groups, key=lambda s: tuple(map(int, s.split("x")))):
            g = groups[k]
            pn, pm, pp = stats([r["psnr_rgb"] for r in g])
            sn, sm, sp = stats([r["ssim_luma"] for r in g])
            L.append(f"| {k} | {len(g)} | {pn:.2f} | {pm:.2f} | {pp:.2f} | {sn:.4f} | {sm:.4f} | {sp:.4f} |")
        L.append("")
        L.append("## PSNR and SSIM by kind")
        L.append("")
        L.append("| kind | n | PSNR RGB min | mean | p5 | SSIM min | mean | p5 |")
        L.append("|---|---|---|---|---|---|---|---|")
        groups = {}
        for r in ok_rows:
            groups.setdefault(r["kind"], []).append(r)
        for k in sorted(groups):
            g = groups[k]
            pn, pm, pp = stats([r["psnr_rgb"] for r in g])
            sn, sm, sp = stats([r["ssim_luma"] for r in g])
            L.append(f"| {k} | {len(g)} | {pn:.2f} | {pm:.2f} | {pp:.2f} | {sn:.4f} | {sm:.4f} | {sp:.4f} |")
        L.append("")
        L.append("## Worst 10 files (by PSNR RGB)")
        L.append("")
        L.append("| file | kind | psnr_rgb | psnr_rgba | ssim | result |")
        L.append("|---|---|---|---|---|---|")
        for r in sorted(ok_rows, key=lambda r: r["psnr_rgb"])[:10]:
            L.append(f"| {r['astc_file']} | {r['kind']} | {r['psnr_rgb']} | {r['psnr_rgba']} | "
                     f"{r['ssim_luma']} | {r['result']} |")
        L.append("")

    failing = [r for r in results if r["result"] == "FAIL"]
    L.append("## Failing rows")
    L.append("")
    if failing:
        for r in failing:
            L.append(f"- {r['astc_file']}: {r['reasons']}")
    else:
        L.append("None.")
    L.append("")

    L.append("## CHECK 5 BCn passthrough")
    L.append("")
    for c in c5:
        L.append(f"- {c['name']}: {'PASS' if c['ok'] else 'FAIL'} ({c['reason']})")
    L.append("")

    L.append("## Throughput (CHECK 6)")
    L.append("")
    for b in benches:
        if b["ok"]:
            L.append(f"- {b['label']} [{b['file']}], --bench 5, threads {b['threads']}, CLI quality {b['quality']}: "
                     f"decode {b['decode']} / encode {b['encode']} / total {b['total']} MPix/s")
        else:
            L.append(f"- {b['label']}, threads {b['threads']}: bench FAILED: {b['line']}")
    L.append("")
    for label in dict.fromkeys(b["label"] for b in benches):
        t16 = next((b for b in benches if b["label"] == label and b["threads"] == 16), None)
        t1 = next((b for b in benches if b["label"] == label and b["threads"] == 1), None)
        if t16 and t1 and t16["ok"] and t1["ok"] and t1["total"]:
            L.append(f"- Scaling {label}: 16 threads / 1 thread = {t16['total'] / t1['total']:.2f}x (total MPix/s)")
    L.append("")
    L.append(f"Cold end-to-end, one CLI process per run, no --bench, --threads 16, {cold['file']} "
             f"(1024x1024 = 1.049 MPix, file IO included):")
    for i, c in enumerate(cold["runs"], 1):
        if c["ok"]:
            L.append(f"- run {i}: {c['secs']:.3f} s wall, {c['mpix_s']:.2f} MPix/s")
        else:
            L.append(f"- run {i}: FAILED {c['line']}")
    okc = [c["mpix_s"] for c in cold["runs"] if c["ok"]]
    if okc:
        L.append(f"- cold MPix/s: min {min(okc):.2f}, max {max(okc):.2f}")
    L.append("")
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")


def sweep(all_rows, args, out, tmp):
    subset = [r for r in all_rows if r["index"] % 4 == 0 and r["w"] == 1024 and r["h"] == 1024]
    sdir = tmp / "sweep"
    sdir.mkdir(parents=True, exist_ok=True)
    bench_row = subset[0]
    table = []
    for q in range(7):
        t0 = time.perf_counter()
        enc_wall = 0.0
        psnrs, ssims, errs = [], [], 0
        for r in subset:
            dds = sdir / f"q{q}_{Path(r['astc_file']).stem}.dds"
            rc, secs, txt = run_cli(encode_args(r, q, 16, dds))
            enc_wall += secs
            if rc != 0:
                errs += 1
                continue
            ref = metrics.load_rgba_png(CORPUS / r["ref_png"])
            dec, _ = metrics.decode_dds(dds, BC7DEC)
            m = metrics.metrics(ref, dec)
            psnrs.append(m["psnr_rgb"])
            ssims.append(m["ssim_luma"])
            dds.unlink(missing_ok=True)
        total = time.perf_counter() - t0
        b = run_bench(bench_row, q, 16, tmp)
        table.append({"q": q, "n": len(psnrs), "errs": errs,
                      "mean_psnr": float(np.mean(psnrs)) if psnrs else float("nan"),
                      "min_psnr": float(np.min(psnrs)) if psnrs else float("nan"),
                      "mean_ssim": float(np.mean(ssims)) if ssims else float("nan"),
                      "enc_wall": enc_wall, "total_wall": total,
                      "bench_enc": b["encode"], "bench_total": b["total"]})
        print(f"  q{q} done", flush=True)

    lines = ["# Quality preset sweep", "",
             f"Subset: every 4th manifest row that is 1024x1024 ({len(subset)} files), --threads 16, "
             f"one process at a time. Bench on {bench_row['astc_file']} (--bench 5 --threads 16).", "",
             "| quality | mean PSNR RGB | min PSNR RGB | mean SSIM | encode wall s | total wall s | "
             "bench encode MPix/s | bench total MPix/s | CLI errors |",
             "|---|---|---|---|---|---|---|---|---|"]
    for t in table:
        lines.append(f"| {t['q']} | {t['mean_psnr']:.3f} | {t['min_psnr']:.3f} | {t['mean_ssim']:.5f} | "
                     f"{t['enc_wall']:.1f} | {t['total_wall']:.1f} | {t['bench_enc']} | {t['bench_total']} | "
                     f"{t['errs']} |")
    text = "\n".join(lines) + "\n"
    (out / "sweep.md").write_text(text, encoding="utf-8")
    print(text)
    shutil.rmtree(sdir, ignore_errors=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--threads-per-job", type=int, default=4)
    ap.add_argument("--quality", type=int, default=None,
                    help="CLI --quality; omitted = do not pass the flag (CLI default)")
    ap.add_argument("--min-psnr-rgb", type=float, default=33.0)
    ap.add_argument("--min-psnr-rgba", type=float, default=33.0)
    ap.add_argument("--min-ssim", type=float, default=0.97)
    ap.add_argument("--min-mean-psnr-rgb", type=float, default=48.0,
                    help="CHECK 7: mean PSNR RGB over all rows must be >= this (default 48)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args(argv)

    out = Path(args.out)
    tmp = out / "tmp"
    dds_dir = out / "dds"
    for d in (out, tmp, dds_dir):
        d.mkdir(parents=True, exist_ok=True)

    all_rows = read_manifest()
    t0 = time.perf_counter()

    if args.sweep:
        rc = sweep(all_rows, args, out, tmp)
        print(f"sweep wall {time.perf_counter() - t0:.1f} s")
        return rc

    rows = all_rows[: args.limit] if args.limit else all_rows
    print(f"oracle: {len(rows)} rows, quality {args.quality if args.quality is not None else 'CLI default'}, "
          f"jobs {args.jobs} x threads {args.threads_per_job}", flush=True)
    results = run_rows(rows, args, tmp, dds_dir)
    write_results(out, results)
    print("CHECK 5 BCn passthrough ...", flush=True)
    c5 = check5(all_rows, args, tmp)
    print("CHECK 6 throughput ...", flush=True)
    benches, cold = throughput(all_rows, args, tmp)
    cli_quality = next((b["quality"] for b in benches if b.get("quality") is not None), None)

    c7 = None
    if args.min_mean_psnr_rgb is not None:
        vals = [r["psnr_rgb"] for r in results if r["psnr_rgb"] != ""]
        mean = float(np.mean(vals)) if vals else float("nan")
        c7 = {"n": len(vals), "mean": mean, "thr": args.min_mean_psnr_rgb,
              "ok": bool(vals) and mean >= args.min_mean_psnr_rgb}
    wall = time.perf_counter() - t0

    nfail = sum(1 for r in results for n in CHECKS if r[f"chk{n}"] == "FAIL")
    nfail += sum(1 for c in c5 if not c["ok"])
    nfail += 1 if (c7 is not None and not c7["ok"]) else 0
    write_report(out, args, results, c5, c7, benches, cold, cli_quality, wall, nfail, len(rows))
    shutil.rmtree(tmp, ignore_errors=True)

    if nfail == 0:
        print(f"ORACLE PASS  (wall {wall:.1f} s)")
        return 0
    print(f"ORACLE FAIL: {nfail} failing checks  (wall {wall:.1f} s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
