#!/usr/bin/env python
"""Encode the CC0 stand-in textures to ASTC LDR at every block size, then decode each
ASTC file back to PNG as ground truth for later PSNR tests.

Run (rerunnable; overwrites outputs in place):
    python ^
        tools\\textures\\scripts\\make_astc_corpus.py --jobs 12
Options: --src <textures_src>  --out <textures_astc>  --astcenc <astcenc-avx2.exe>
         --jobs N   --clean (delete --out first)   --verify-only (check existing output)

Inputs : <src>\\MANIFEST.tsv and the PNGs it lists (ambientCG CC0 set).
Outputs: <out>\\
  <asset>_<map>__<WxH>__b<bw>x<bh>__<srgb|lin>.astc   astcenc -cs/-cl <blk> -medium -j 1
  ref_png\\<same base>.png                              astcenc -ds/-dl (ground truth)
  inputs\\<asset>_<map>.png                             8-bit RGB/RGBA copy fed to astcenc
  crops\\<asset>_<map>__<WxH>.png                       top-left odd-size crops
  MANIFEST.tsv   columns: astc_file, ref_png (relative to <out>), source_png (absolute),
                 kind, width, height, block_w, block_h, srgb, has_alpha, astc_bytes, encode_seconds

Conversion (Pillow, before encoding):
  - 16-bit gray (I;16): value / 257 -> 8-bit L (full 0..65535 range maps linearly to 0..255),
    then RGB.
  - Any mode with an alpha channel (RGBA, LA, PA, P+transparency): RGBA, alpha kept.
  - Everything else (RGB, L, P): RGB.
  - has_alpha = 1 when the encoded input has an alpha channel with at least one pixel < 255.
Encoding: kind albedo/noise/alpha -> sRGB (-cs, srgb=1); kind normal/gray -> linear (-cl, srgb=0).
Matrix: 16 full-size images x 14 block sizes, plus 3 assets x 3 crop sizes x 4 block sizes.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

def _project_root():
    """The project folder: NXRT_ROOT, else the nearest parent of this file that holds both tools/ and work/."""
    import os, pathlib
    env = os.environ.get('NXRT_ROOT')
    if env:
        return pathlib.Path(env).as_posix()
    here = pathlib.Path(__file__).resolve()
    for p in here.parents:
        if (p / 'tools').is_dir() and (p / 'work').is_dir():
            return p.as_posix()
    return here.parents[2].as_posix()


ROOT = Path(_project_root()) / "work" / "corpus"
DEFAULT_SRC = ROOT / "textures_src"
DEFAULT_OUT = ROOT / "textures_astc"
DEFAULT_ASTCENC = ROOT / "astcenc" / "bin" / "astcenc-avx2.exe"

BLOCKS_FULL = ["4x4", "5x4", "5x5", "6x5", "6x6", "8x5", "8x6", "8x8",
               "10x5", "10x6", "10x8", "10x10", "12x10", "12x12"]
BLOCKS_CROP = ["4x4", "6x6", "8x8", "12x12"]
CROP_SIZES = [(1000, 750), (333, 257), (31, 17)]
# one albedo, one normal, one with real alpha
CROP_ASSETS = ["Bricks113A_Color", "Tiles074_NormalGL", "ScatteredLeaves001_Color"]
SRGB_KINDS = {"albedo", "noise", "alpha"}
MAGIC = bytes([0x13, 0xAB, 0xA1, 0x5C])
COLUMNS = ["astc_file", "ref_png", "source_png", "kind", "width", "height",
           "block_w", "block_h", "srgb", "has_alpha", "astc_bytes", "encode_seconds"]
NAME_RE = re.compile(r"^(?P<asset>.+?)_1K-PNG_(?P<map>.+)\.png$")
NAME_WH_RE = re.compile(r"__(\d+)x(\d+)__b\d+x\d+__")
BLOCK_RE = re.compile(r"__b(\d+)x(\d+)__")


def to_8bit_input(img: Image.Image) -> Image.Image:
    if img.mode in ("I;16", "I;16B", "I"):
        img = img.convert("I").point(lambda v: v / 257.0).convert("L")
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        return img.convert("RGBA")
    if img.mode == "P" and "transparency" in img.info:
        return img.convert("RGBA")
    return img.convert("RGB")


def has_real_alpha(img: Image.Image) -> bool:
    return img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 255


def prepare(src: Path, out: Path, manifest_rows: list[dict]) -> list[dict]:
    """Write 8-bit inputs and crops; return one item per (image, size)."""
    items = []
    for r in manifest_rows:
        m = NAME_RE.match(Path(r["file"]).name)
        if not m:
            raise SystemExit(f"cannot parse asset/map from {r['file']}")
        base = f"{m.group('asset')}_{m.group('map')}"
        src_png = src / r["file"]
        with Image.open(src_png) as im:
            img = to_8bit_input(im)
        prepared = out / "inputs" / f"{base}.png"
        img.save(prepared)
        items.append(dict(base=base, kind=r["kind"], w=img.width, h=img.height,
                          input=prepared, source=src_png, alpha=has_real_alpha(img),
                          blocks=BLOCKS_FULL))
        if base in CROP_ASSETS:
            for cw, ch in CROP_SIZES:
                crop = img.crop((0, 0, cw, ch))
                cp = out / "crops" / f"{base}__{cw}x{ch}.png"
                crop.save(cp)
                items.append(dict(base=base, kind=r["kind"], w=cw, h=ch, input=cp, source=cp,
                                  alpha=has_real_alpha(crop), blocks=BLOCKS_CROP))
    return items


def build_jobs(items: list[dict], out: Path) -> list[dict]:
    jobs = []
    for it in items:
        srgb = 1 if it["kind"] in SRGB_KINDS else 0
        tag = "srgb" if srgb else "lin"
        for blk in it["blocks"]:
            bw, bh = blk.split("x")
            stem = f"{it['base']}__{it['w']}x{it['h']}__b{blk}__{tag}"
            jobs.append(dict(stem=stem, input=it["input"], source=it["source"], kind=it["kind"],
                             w=it["w"], h=it["h"], block=blk, bw=bw, bh=bh, srgb=srgb,
                             alpha=it["alpha"], astc=out / f"{stem}.astc",
                             ref=out / "ref_png" / f"{stem}.png"))
    return jobs


def encode_one(astcenc: str, job: dict):
    enc_flag = "-cs" if job["srgb"] else "-cl"
    dec_flag = "-ds" if job["srgb"] else "-dl"
    t0 = time.perf_counter()
    p = subprocess.run([astcenc, enc_flag, str(job["input"]), str(job["astc"]), job["block"],
                        "-medium", "-j", "1"], capture_output=True, text=True)
    secs = time.perf_counter() - t0
    if p.returncode:
        return job, None, f"encode rc={p.returncode}: {(p.stdout + p.stderr)[-400:]}"
    d = subprocess.run([astcenc, dec_flag, str(job["astc"]), str(job["ref"]), "-j", "1"],
                       capture_output=True, text=True)
    if d.returncode:
        return job, None, f"decode rc={d.returncode}: {(d.stdout + d.stderr)[-400:]}"
    return job, secs, None


def astc_header(path: Path) -> tuple[int, int, int, int, int]:
    h = path.read_bytes()[:16]
    if h[:4] != MAGIC:
        raise ValueError("bad magic")
    return (h[4], h[5], h[6], int.from_bytes(h[7:10], "little"), int.from_bytes(h[10:13], "little"))


def verify(out: Path, rows: list[dict]) -> list[str]:
    errors = []
    on_disk = {p.name for p in out.glob("*.astc")}
    listed = {r["astc_file"] for r in rows}
    if on_disk != listed:
        errors.append(f"count mismatch: on disk {len(on_disk)}, manifest {len(listed)}, "
                      f"extra={sorted(on_disk - listed)[:5]}, missing={sorted(listed - on_disk)[:5]}")
    for r in rows:
        p = out / r["astc_file"]
        try:
            bw, bh, bz, W, H = astc_header(p)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{r['astc_file']}: header read failed: {e}")
            continue
        m = BLOCK_RE.search(r["astc_file"])
        wh = NAME_WH_RE.search(r["astc_file"])
        if (bw, bh, bz) != (int(m.group(1)), int(m.group(2)), 1):
            errors.append(f"{r['astc_file']}: header block {bw}x{bh}x{bz} != name")
        if (bw, bh) != (int(r["block_w"]), int(r["block_h"])):
            errors.append(f"{r['astc_file']}: header block != manifest block_w/block_h")
        if (W, H) != (int(wh.group(1)), int(wh.group(2))) or (W, H) != (int(r["width"]), int(r["height"])):
            errors.append(f"{r['astc_file']}: header size {W}x{H} != name/manifest")
        expected = 16 + 16 * math.ceil(W / bw) * math.ceil(H / bh)
        if p.stat().st_size != expected:
            errors.append(f"{r['astc_file']}: size {p.stat().st_size} != expected {expected}")
        ref = out / r["ref_png"]
        if not ref.exists():
            errors.append(f"{r['ref_png']}: missing")
        else:
            with Image.open(ref) as im:
                if im.size != (W, H):
                    errors.append(f"{r['ref_png']}: size {im.size} != {(W, H)}")
    # open five decoded PNGs fully, spread across the manifest
    n = len(rows)
    picks = sorted({0, n // 4, n // 2, (3 * n) // 4, n - 1})[:5]
    for i in picks:
        r = rows[i]
        with Image.open(out / r["ref_png"]) as im:
            im.load()
            print(f"  spot-check {r['ref_png']}: mode={im.mode} size={im.size[0]}x{im.size[1]}")
            if im.size != (int(r["width"]), int(r["height"])):
                errors.append(f"spot-check {r['ref_png']}: size mismatch")
    return errors


def read_tsv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--astcenc", default=str(DEFAULT_ASTCENC))
    ap.add_argument("--jobs", type=int, default=12, help="parallel astcenc processes (each -j 1)")
    ap.add_argument("--clean", action="store_true", help="delete --out before running")
    ap.add_argument("--verify-only", action="store_true", help="only verify existing output")
    args = ap.parse_args()

    src, out = Path(args.src), Path(args.out)
    t_start = time.perf_counter()

    if args.verify_only:
        rows = read_tsv(out / "MANIFEST.tsv")
        errors = verify(out, rows)
        print("\n".join(errors) if errors else f"verify OK: {len(rows)} rows")
        return 1 if errors else 0

    if args.clean and out.exists():
        shutil.rmtree(out)
    for d in (out, out / "ref_png", out / "inputs", out / "crops"):
        d.mkdir(parents=True, exist_ok=True)

    items = prepare(src, out, read_tsv(src / "MANIFEST.tsv"))
    jobs = build_jobs(items, out)
    print(f"{len(items)} images/crops -> {len(jobs)} encodes, {args.jobs} parallel", flush=True)

    results, failures = [], []
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(encode_one, args.astcenc, j) for j in jobs]
        for done, fut in enumerate(as_completed(futs), 1):
            job, secs, err = fut.result()
            if err:
                failures.append((job["stem"], err))
            else:
                results.append((job, secs))
            if done % 20 == 0 or done == len(jobs):
                print(f"[{done}/{len(jobs)}] encoded+decoded", flush=True)

    if failures:
        for stem, err in failures:
            print(f"FAILED {stem}: {err}", file=sys.stderr)
        print(f"{len(failures)} failures; manifest not written", file=sys.stderr)
        return 1

    rows = []
    for job, secs in results:
        rows.append({
            "astc_file": job["astc"].name,
            "ref_png": f"ref_png/{job['ref'].name}",
            "source_png": str(job["source"]),
            "kind": job["kind"],
            "width": job["w"],
            "height": job["h"],
            "block_w": job["bw"],
            "block_h": job["bh"],
            "srgb": job["srgb"],
            "has_alpha": int(job["alpha"]),
            "astc_bytes": job["astc"].stat().st_size,
            "encode_seconds": f"{secs:.3f}",
        })
    rows.sort(key=lambda r: r["astc_file"])
    with open(out / "MANIFEST.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    errors = verify(out, rows)
    total_bytes = sum(r["astc_bytes"] for r in rows)
    print(f"rows={len(rows)} astc_bytes_total={total_bytes} "
          f"wall_seconds={time.perf_counter() - t_start:.1f}")
    if errors:
        print("VERIFY ERRORS:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print("verify OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
