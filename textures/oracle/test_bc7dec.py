"""Test for bc7dec.exe: builds DDS files with known BC7 mode-6 solid-colour blocks and checks the decode.

Run:  python test_bc7dec.py [--bc7dec <exe>]   (from the project root)

Layout of each block is taken from the reference decoder's bitfield struct (bc7decomp_ref.cpp, bc7_mode_6):
  bits 0-6 mode (value 1<<6), then R0 R1 G0 G1 B0 B1 A0 A1 (7 bits each), P0 (1 bit),
  then P1 (1 bit), then 63 index bits (all zero here).
Endpoint value = (7-bit << 1) | pbit. With all indices 0 the decoded colour equals endpoint 0 exactly.
Each endpoint's single P-bit is shared by all four channels, so colours are chosen with channel LSBs
equal to the P-bit.
"""

import argparse
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_EXE = HERE.parent / "build" / "oracle" / "bc7dec.exe"
OUT_DIR = HERE.parent / "build" / "oracle" / "selftest"

DXGI_BC7_UNORM = 98
DXGI_BC7_UNORM_SRGB = 99
DXGI_BC1_UNORM = 71
DDPF_FOURCC = 0x4


def mode6_solid_block(rgba):
    """Return 16 bytes of a mode-6 block whose every pixel decodes to rgba (r, g, b, a in 0..255).

    Requires every channel's LSB to equal the endpoint P-bit (P = LSB of R, which must equal LSB of G, B, A).
    """
    r, g, b, a = rgba
    p = r & 1
    if not (g & 1 == p and b & 1 == p and a & 1 == p):
        raise ValueError(f"channel LSBs must match the P-bit in {rgba}")
    bits = 0
    pos = 0

    def put(value, n):
        nonlocal bits, pos
        bits |= (value & ((1 << n) - 1)) << pos
        pos += n

    put(1 << 6, 7)  # mode 6
    for c in (r, g, b, a):  # R0 R1 G0 G1 B0 B1 A0 A1 ; both endpoints equal
        put(c >> 1, 7)
        put(c >> 1, 7)
    put(p, 1)  # P0
    put(p, 1)  # P1
    put(0, 63)  # index bits: all zero, so every pixel uses index 0 -> endpoint 0 exactly
    assert pos == 128
    return bits.to_bytes(16, "little")


def dds_bc7(width, height, blocks, dxgi=DXGI_BC7_UNORM):
    """Wrap raw BC7 block bytes in a DDS file with a DX10 extension header (mip 0 only)."""
    header = struct.pack(
        "<IIIIIII",
        124,  # dwSize
        0x1 | 0x2 | 0x4 | 0x1000,  # DDSD_CAPS | HEIGHT | WIDTH | PIXELFORMAT
        height,
        width,
        0,  # pitch
        0,  # depth
        1,  # mip count
    )
    header += b"\0" * 44  # dwReserved1[11]
    header += struct.pack("<II4sIIIII", 32, DDPF_FOURCC, b"DX10", 0, 0, 0, 0, 0)  # DDS_PIXELFORMAT
    header += struct.pack("<IIIII", 0x1000, 0, 0, 0, 0)  # caps, caps2, caps3, caps4, reserved2
    assert len(header) == 124
    dx10 = struct.pack("<IIIII", dxgi, 3, 0, 1, 0)  # TEXTURE2D, arraySize 1
    return b"DDS " + header + dx10 + b"".join(blocks)


def run_case(exe, name, width, height, block_colours, dxgi, expect_srgb, out_dir):
    """block_colours: dict (bx, by) -> rgba. Blocks are row-major over ceil(w/4) x ceil(h/4)."""
    bw, bh = (width + 3) // 4, (height + 3) // 4
    blocks = [mode6_solid_block(block_colours[(i % bw, i // bw)]) for i in range(bw * bh)]
    dds = out_dir / f"{name}.dds"
    rgba = out_dir / f"{name}.rgba"
    dds.write_bytes(dds_bc7(width, height, blocks, dxgi))
    proc = subprocess.run([str(exe), str(dds), str(rgba)], capture_output=True, text=True)
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {proc.stderr.strip()}"
    data = rgba.read_bytes()
    w, h, srgb = struct.unpack("<III", data[:12])
    if (w, h, srgb) != (width, height, expect_srgb):
        return False, f"header mismatch: got {w}x{h} srgb={srgb}"
    pix = data[12:]
    if len(pix) != width * height * 4:
        return False, f"pixel byte count {len(pix)} != {width * height * 4}"
    bad = 0
    first_bad = None
    for y in range(height):
        for x in range(width):
            want = block_colours[(x // 4, y // 4)]
            got = tuple(pix[(y * width + x) * 4:(y * width + x) * 4 + 4])
            if got != tuple(want):
                bad += 1
                if first_bad is None:
                    first_bad = (x, y, got, tuple(want))
    if bad:
        return False, f"{bad} pixels differ; first at (x,y)={first_bad[0]},{first_bad[1]} got {first_bad[2]} want {first_bad[3]}"
    return True, f"{width}x{height} all {width * height} pixels match"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bc7dec", default=str(DEFAULT_EXE))
    args = ap.parse_args()
    exe = Path(args.bc7dec)
    if not exe.exists():
        print(f"bc7dec not found: {exe}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 6x5 image = 2x2 blocks, with partial blocks on the right (x 4..5 only) and bottom (y 4 only).
    # Block colours: (201,101,51,255) all-odd with alpha 255; (200,100,50,254) all-even with alpha 254, etc.
    colours = {
        (0, 0): (201, 101, 51, 255),
        (1, 0): (200, 100, 50, 254),
        (0, 1): (13, 77, 199, 255),
        (1, 1): (0, 2, 4, 0),
    }
    cases = [
        ("unorm_6x5", 6, 5, colours, DXGI_BC7_UNORM, 0),
        ("srgb_6x5", 6, 5, colours, DXGI_BC7_UNORM_SRGB, 1),
    ]
    failures = 0
    for name, w, h, cols, dxgi, srgb in cases:
        ok, msg = run_case(exe, name, w, h, cols, dxgi, srgb, OUT_DIR)
        print(f"{'PASS' if ok else 'FAIL'} {name}: {msg}")
        failures += 0 if ok else 1

    # Negative case: a BC1 DXGI format must be rejected (non-zero exit, no output).
    neg_dds = OUT_DIR / "bc1_reject.dds"
    neg_rgba = OUT_DIR / "bc1_reject.rgba"
    neg_rgba.unlink(missing_ok=True)
    neg_dds.write_bytes(dds_bc7(4, 4, [b"\0" * 8], DXGI_BC1_UNORM))
    proc = subprocess.run([str(exe), str(neg_dds), str(neg_rgba)], capture_output=True, text=True)
    ok = proc.returncode != 0 and not neg_rgba.exists()
    print(f"{'PASS' if ok else 'FAIL'} reject_bc1: exit {proc.returncode}: {proc.stderr.strip()}")
    failures += 0 if ok else 1

    print("ALL PASS" if failures == 0 else f"{failures} FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
