"""Quality metrics for the ASTC->BC7 oracle: decode a BC7 DDS with bc7dec, compare to a reference RGBA PNG.

CLI:
  metrics.py --ref <ref.png> --dds <out.dds> [--bc7dec <exe>] [--json]
  metrics.py --selftest

API:
  from metrics import compare
  compare(ref_png, dds, bc7dec=None) -> dict

Metrics (8-bit, peak 255):
  psnr_rgba   PSNR over all 4 channels jointly (MSE averaged over pixels and channels)
  psnr_r/g/b/a  per-channel PSNR
  psnr_rgb    PSNR over R,G,B jointly
  ssim_luma   SSIM on Rec.601 luma of RGB (Gaussian window, sigma 1.5, 11x11 support, K1=0.01, K2=0.03)
Identical inputs (MSE == 0) report PSNR 99.0.
"""

import argparse
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

HERE = Path(__file__).resolve().parent
DEFAULT_BC7DEC = HERE.parent / "build" / "oracle" / "bc7dec.exe"

PEAK = 255.0
IDENTICAL_PSNR = 99.0
SSIM_SIGMA = 1.5
SSIM_WINDOW = 11
SSIM_K1 = 0.01
SSIM_K2 = 0.03


# ---------- loading ----------

def load_rgba_png(path):
    """Reference PNG as H x W x 4 uint8. RGB-only PNGs get alpha 255."""
    img = Image.open(path)
    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")
    return np.asarray(img.convert("RGBA"), dtype=np.uint8)


def decode_dds(dds, bc7dec=None):
    """Run bc7dec on a DDS file and return (H x W x 4 uint8, srgb flag)."""
    exe = Path(bc7dec) if bc7dec else DEFAULT_BC7DEC
    if not exe.exists():
        raise FileNotFoundError(f"bc7dec not found: {exe}")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "decoded.rgba"
        proc = subprocess.run(
            [str(exe), str(Path(dds).resolve()), str(out)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"bc7dec failed (exit {proc.returncode}): {proc.stderr.strip()}")
        data = out.read_bytes()
    w, h, srgb = struct.unpack("<III", data[:12])
    pix = np.frombuffer(data[12:], dtype=np.uint8)
    if pix.size != w * h * 4:
        raise RuntimeError(f"bc7dec output size {pix.size} does not match {w}x{h}x4")
    return pix.reshape(h, w, 4), bool(srgb)


# ---------- metrics ----------

def _psnr_from_mse(mse):
    if mse == 0.0:
        return IDENTICAL_PSNR
    return float(10.0 * np.log10(PEAK * PEAK / mse))


def psnr(a, b, channels=None):
    """PSNR between two uint8 arrays (H x W x C). channels: optional list of channel indices."""
    x = a.astype(np.float64)
    y = b.astype(np.float64)
    if channels is not None:
        x = x[..., channels]
        y = y[..., channels]
    return _psnr_from_mse(float(np.mean((x - y) ** 2)))


def luma601(rgba):
    """Rec.601 luma of the RGB channels, float64 in 0..255."""
    rgb = rgba[..., :3].astype(np.float64)
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def ssim(x, y):
    """Mean SSIM of two 2-D float arrays in 0..255, Gaussian window (sigma 1.5, truncate 5 px => 11x11)."""
    c1 = (SSIM_K1 * PEAK) ** 2
    c2 = (SSIM_K2 * PEAK) ** 2
    truncate = ((SSIM_WINDOW - 1) / 2.0) / SSIM_SIGMA  # 5 / 1.5 -> radius 5 -> 11 taps
    g = lambda z: gaussian_filter(z, sigma=SSIM_SIGMA, truncate=truncate, mode="reflect")
    mu_x = g(x)
    mu_y = g(y)
    var_x = g(x * x) - mu_x * mu_x
    var_y = g(y * y) - mu_y * mu_y
    cov_xy = g(x * y) - mu_x * mu_y
    num = (2.0 * mu_x * mu_y + c1) * (2.0 * cov_xy + c2)
    den = (mu_x * mu_x + mu_y * mu_y + c1) * (var_x + var_y + c2)
    return float(np.mean(num / den))


def metrics(ref, dec):
    """ref, dec: H x W x 4 uint8 arrays of identical shape."""
    out = {"psnr_rgba": psnr(ref, dec)}
    for i, name in enumerate("rgba"):
        out[f"psnr_{name}"] = psnr(ref, dec, [i])
    out["psnr_rgb"] = psnr(ref, dec, [0, 1, 2])
    out["ssim_luma"] = ssim(luma601(ref), luma601(dec))
    return out


# ---------- API ----------

def compare(ref_png, dds, bc7dec=None):
    """Decode dds with bc7dec, compare with ref_png. Returns a dict of metrics plus sizes and srgb flag.

    Raises ValueError if the dimensions differ.
    """
    ref = load_rgba_png(ref_png)
    dec, srgb = decode_dds(dds, bc7dec)
    rh, rw = ref.shape[:2]
    dh, dw = dec.shape[:2]
    if (rw, rh) != (dw, dh):
        raise ValueError(f"dimension mismatch: ref {rw}x{rh} vs dds {dw}x{dh}")
    result = {"width": rw, "height": rh, "srgb": srgb, "identical": bool(np.array_equal(ref, dec))}
    result.update(metrics(ref, dec))
    return result


# ---------- selftest ----------

def _selftest():
    rng = np.random.default_rng(1234)
    ok_all = True

    def check(name, cond, detail):
        nonlocal ok_all
        ok_all &= bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}: {detail}")

    # smooth, mid-range test image so that noise does not hit the 0/255 clip
    base = gaussian_filter(rng.random((256, 256, 4)), sigma=(3, 3, 0))
    base = (base - base.min()) / (base.max() - base.min())
    img = np.rint(64 + base * 127).astype(np.uint8)  # values in 64..191

    # (1) identical
    p = psnr(img, img.copy())
    s = ssim(luma601(img), luma601(img.copy()))
    check("identical", p == IDENTICAL_PSNR and abs(s - 1.0) < 1e-9, f"PSNR={p} SSIM={s:.12f} (want 99.0 / 1.0)")

    # (2) gaussian noise sigma 5, rounded to 8-bit
    sigma = 5.0
    noisy = np.clip(np.rint(img.astype(np.float64) + rng.normal(0.0, sigma, img.shape)), 0, 255).astype(np.uint8)
    analytic = 20.0 * np.log10(PEAK / sigma)  # 34.15 dB
    p = psnr(img, noisy)
    check("noise_sigma5", abs(p - analytic) <= 0.2, f"PSNR={p:.3f} analytic={analytic:.3f} diff={p - analytic:+.3f}")

    # (3) blurred copy: SSIM strictly below 1
    blurred = np.rint(gaussian_filter(img.astype(np.float64), sigma=(2, 2, 0))).astype(np.uint8)
    s = ssim(luma601(img), luma601(blurred))
    check("blur_ssim_below_1", s < 1.0 and s > 0.0, f"SSIM={s:.6f}")

    # optional cross-check against skimage if it happens to be installed (not required)
    try:
        from skimage.metrics import structural_similarity as sk_ssim  # noqa: F401
        sk = sk_ssim(luma601(img), luma601(blurred), data_range=255.0, gaussian_weights=True,
                     sigma=SSIM_SIGMA, use_sample_covariance=False)
        print(f"INFO skimage cross-check (blur pair): SSIM={sk:.6f} (not asserted; boundary handling may differ)")
    except ImportError:
        print("INFO skimage not installed; cross-check skipped")

    print("SELFTEST PASS" if ok_all else "SELFTEST FAILED")
    return 0 if ok_all else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", help="reference RGBA PNG")
    ap.add_argument("--dds", help="BC7 DDS to decode and compare")
    ap.add_argument("--bc7dec", default=str(DEFAULT_BC7DEC), help="path to bc7dec.exe")
    ap.add_argument("--json", action="store_true", help="print JSON instead of one line")
    ap.add_argument("--selftest", action="store_true", help="run the metric self-test and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.ref or not args.dds:
        ap.error("--ref and --dds are required (or use --selftest)")

    try:
        res = compare(args.ref, args.dds, args.bc7dec)
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(
            f"{res['width']}x{res['height']} srgb={int(res['srgb'])} "
            f"psnr_rgba={res['psnr_rgba']:.4f} psnr_r={res['psnr_r']:.4f} psnr_g={res['psnr_g']:.4f} "
            f"psnr_b={res['psnr_b']:.4f} psnr_a={res['psnr_a']:.4f} psnr_rgb={res['psnr_rgb']:.4f} "
            f"ssim_luma={res['ssim_luma']:.6f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
