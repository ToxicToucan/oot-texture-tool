# SPDX-License-Identifier: GPL-3.0-or-later
"""Texture exporter / importer for Ocarina of Time (Switch 2) mods.

  python texture_tool.py export <romfs\\Tex\\name.bntx.zs> <out.png>
  python texture_tool.py import <romfs\\Tex\\name.bntx.zs> <edited.png> <mod folder>

export: the texture's full-size image (mip 0) as a PNG. ASTC and BC1..BC7 textures.
import: the PNG (same size as the texture) back into a copy of the texture under <mod folder>\\romfs\\Tex\\, every mip level
        made from it, for the menu's Mods tab. This version imports into BC7 textures (the file keeps its size, so the game's
        resource size table needs no change); ASTC textures are exported fine but not yet imported.

Runs on Python 3 with Pillow; the zstd step runs zs_helper.py on Python 3.14+ (compression.zstd; PY_ZSTD, default
"py -3.15"). texconv.exe (build-texconv.cmd) next to this file or in TEXCONV_DIR. The zstd dictionaries come from the game's own Pack/ZsDic.pack.zs. Nothing in the game folder is written.
The block-linear layout: GOBs of 64 bytes x 8 rows stacked in blocks of 2^n GOBs (as in the public Tegra descriptions), with the
order inside a GOB measured on this game's textures (see swizzle_offsets). The BNTX fields follow Switch-Toolbox's notes.
"""
import io
import math
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEXCONV = os.path.join(os.environ.get('TEXCONV_DIR', HERE), 'texconv.exe')


class ToolError(Exception):
    """A problem to show the user as it is (the command line prints it; the window shows it in a message box)."""

ASTC_BLOCKS = {0x2d: (4, 4), 0x2e: (5, 4), 0x2f: (5, 5), 0x30: (6, 5), 0x31: (6, 6), 0x32: (8, 5), 0x33: (8, 6), 0x34: (8, 8),
               0x35: (10, 5), 0x36: (10, 6), 0x37: (10, 8), 0x38: (10, 10), 0x39: (12, 10), 0x3a: (12, 12)}
BC = {0x1a: ('BC1', 8, 71), 0x1b: ('BC2', 16, 74), 0x1c: ('BC3', 16, 77), 0x1d: ('BC4', 8, 80), 0x1e: ('BC5', 16, 83),
      0x1f: ('BC6H', 16, 95), 0x20: ('BC7', 16, 98)}   # (name, bytes per block, DXGI format)


def find_romfs(path):
    """The romfs folder the file is in (its path inside the game), and the game's romfs with the zstd dictionaries."""
    p, inside = os.path.abspath(path), None
    while True:
        if os.path.basename(p).lower() == 'romfs':
            inside = p
            break
        q = os.path.dirname(p)
        if q == p:
            break
        p = q
    return inside


def game_romfs(inside):
    for cand in (inside, os.environ.get('OOT_ROMFS', ''), os.path.join('C:' + os.sep, 'Switch2', 'oot-recomp-probe', 'romfs')):
        if cand and os.path.exists(os.path.join(cand, 'Pack', 'ZsDic.pack.zs')):
            return cand
    raise ToolError('the game romfs (Pack/ZsDic.pack.zs) was not found: set OOT_ROMFS')


PY_ZSTD = os.environ.get('PY_ZSTD', 'py -3.15').split()
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)   # no console flashing when run from the window (pythonw)


def unzs(path, romfs, tmp):
    out = os.path.join(tmp, 'tex.bin')
    r = subprocess.run(PY_ZSTD + ['-I', os.path.join(HERE, 'zs_helper.py'), 'd', romfs, path, out], capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError('zstd: ' + r.stderr.strip())
    return open(out, 'rb').read(), int(r.stdout.strip())


def zs(data, romfs, dict_index, dst, tmp):
    src = os.path.join(tmp, 'new.bin')
    open(src, 'wb').write(data)
    r = subprocess.run(PY_ZSTD + ['-I', os.path.join(HERE, 'zs_helper.py'), 'c', romfs, src, dst, str(dict_index)], capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError('zstd: ' + r.stderr.strip())


class Texture:
    def __init__(self, data):
        self.d = bytearray(data)
        i = self.d.find(b'BRTI')
        if i < 0:
            raise ValueError('not a BNTX texture')
        self.brti = i
        _, _, self.tile, _, self.mips = struct.unpack_from('<BBHHH', self.d, i + 0x10)
        self.fmt = struct.unpack_from('<I', self.d, i + 0x1C)[0]
        self.w, self.h, self.depth, self.layers = struct.unpack_from('<IIII', self.d, i + 0x24)
        self.bh_log2 = struct.unpack_from('<I', self.d, i + 0x34)[0]
        self.size = struct.unpack_from('<I', self.d, i + 0x50)[0]
        ptrs = struct.unpack_from('<Q', self.d, i + 0x70)[0]
        self.mip_offsets = [struct.unpack_from('<Q', self.d, ptrs + 8 * k)[0] for k in range(self.mips)]
        self.data_start = self.mip_offsets[0]
        self.kind = self.fmt >> 8
        self.srgb = (self.fmt & 0xff) == 0x06
        if self.kind in ASTC_BLOCKS:
            self.bw, self.bh = ASTC_BLOCKS[self.kind]
            self.bpb = 16
        elif self.kind in BC:
            self.bw = self.bh = 4
            self.bpb = BC[self.kind][1]
        else:
            raise ValueError('format 0x%x is not ASTC or BC1..BC7' % self.fmt)

    def mip_dims(self, m):
        w, h = max(1, self.w >> m), max(1, self.h >> m)
        return w, h, (w + self.bw - 1) // self.bw, (h + self.bh - 1) // self.bh

    def gobs_y(self, m):
        g = 1 << self.bh_log2
        hb = self.mip_dims(m)[3]
        while g > 1 and hb <= (g >> 1) * 8:
            g >>= 1
        return g

    def mip_region(self, m):
        a = self.mip_offsets[m]
        b = self.mip_offsets[m + 1] if m + 1 < self.mips else self.data_start + self.size
        return a, b


def swizzle_offsets(wb, hb, bpb, gobs_y):
    """The byte offset in block-linear memory of each block (row by row), for a surface of wb x hb blocks of bpb bytes."""
    width_bytes = wb * bpb
    gobs_x = (width_bytes + 63) // 64
    block_h = 8 * gobs_y
    out = []
    for y in range(hb):
        row_base = (y // block_h) * gobs_x * 512 * gobs_y + ((y % block_h) // 8) * 512
        yg = y % 8
        for x in range(wb):
            xb = x * bpb
            off = row_base + (xb // 64) * 512 * gobs_y
            xg = xb % 64
            # Inside a 512-byte GOB (64 bytes x 8 rows) the Switch 2 order, found by testing all 120 bit orders on the
            # game's own textures (only this one gives clean images): byte x bits 0..3, then row bit 0, row bit 1, x bit 4,
            # row bit 2, x bit 5.
            off += (xg % 16) + (yg & 1) * 16 + ((yg >> 1) & 1) * 32 + ((xg >> 4) & 1) * 64 + ((yg >> 2) & 1) * 128 + ((xg >> 5) & 1) * 256
            out.append(off)
    return out


def read_mip_blocks(t, m):
    w, h, wb, hb = t.mip_dims(m)
    a, b = t.mip_region(m)
    region = t.d[a:b]
    out = bytearray(wb * hb * t.bpb)
    for k, off in enumerate(swizzle_offsets(wb, hb, t.bpb, t.gobs_y(m))):
        out[k * t.bpb:(k + 1) * t.bpb] = region[off:off + t.bpb]
    return bytes(out)


def write_mip_blocks(t, m, blocks):
    w, h, wb, hb = t.mip_dims(m)
    a, b = t.mip_region(m)
    for k, off in enumerate(swizzle_offsets(wb, hb, t.bpb, t.gobs_y(m))):
        if a + off + t.bpb > b:
            raise ValueError('mip %d: block outside its region' % m)
        t.d[a + off:a + off + t.bpb] = blocks[k * t.bpb:(k + 1) * t.bpb]


def run(*args):
    if not os.path.exists(TEXCONV):
        raise ToolError('texconv.exe was not found at %s: build it with build-texconv.cmd and set TEXCONV_DIR' % TEXCONV)
    r = subprocess.run([TEXCONV] + [str(x) for x in args], capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError('texconv: ' + (r.stderr or r.stdout).strip())


def format_name(t):
    return ('ASTC %dx%d' % (t.bw, t.bh)) if t.kind in ASTC_BLOCKS else BC[t.kind][0]


def decode_mip0(src):
    """The texture and its full-size image (mip 0) as a Pillow RGBA image."""
    from PIL import Image
    romfs = game_romfs(find_romfs(src))
    with tempfile.TemporaryDirectory() as tmp:
        d, _ = unzs(src, romfs, tmp)
        t = Texture(d)
        w, h, wb, hb = t.mip_dims(0)
        blocks = read_mip_blocks(t, 0)
        if t.kind in ASTC_BLOCKS:
            bp, rp = os.path.join(tmp, 'b.bin'), os.path.join(tmp, 'p.rgba')
            open(bp, 'wb').write(blocks)
            run('decode-astc', bp, rp, w, h, t.bw, t.bh, 'srgb' if t.srgb else 'linear')
            img = Image.frombytes('RGBA', (w, h), open(rp, 'rb').read())
        else:
            dxgi = BC[t.kind][2] + (1 if t.srgb and t.kind in (0x1a, 0x1b, 0x1c, 0x20) else 0)
            hdr = b'DDS ' + struct.pack('<7I44x', 124, 0x1007, h, w, len(blocks), 0, 1) + struct.pack('<2I4s20x', 32, 4, b'DX10')
            hdr += struct.pack('<5I', 0x1000, 0, 0, 0, 0) + struct.pack('<5I', dxgi, 3, 0, 1, 0)
            img = Image.open(io.BytesIO(hdr + blocks)).convert('RGBA')
    return t, img


def export_png(src, png):
    t, img = decode_mip0(src)
    img.save(png)
    print('%s: %s %dx%d, %d mip levels%s -> %s' % (os.path.basename(src), format_name(t), t.w, t.h, t.mips,
                                                   ' sRGB' if t.srgb else '', png))
    return t


def import_png(src, png, mod):
    from PIL import Image
    inside = find_romfs(src)
    romfs = game_romfs(inside)
    tmp_obj = tempfile.TemporaryDirectory()
    tmp0 = tmp_obj.name
    d, dct = unzs(src, romfs, tmp0)
    t = Texture(d)
    if t.kind != 0x20:
        raise ToolError('%s is %s: this version imports into BC7 textures only (export works for all)' %
                         (os.path.basename(src), format_name(t)))
    img = Image.open(png).convert('RGBA')
    if img.size != (t.w, t.h):
        raise ToolError('the PNG is %dx%d; the texture is %dx%d' % (img.size[0], img.size[1], t.w, t.h))
    with tempfile.TemporaryDirectory() as tmp:
        for m in range(t.mips):
            w, h, wb, hb = t.mip_dims(m)
            level = img if m == 0 else img.resize((w, h), Image.LANCZOS)
            # BC7 works on 4x4 blocks: a level smaller than a block is padded (its edge repeated)
            pw, ph = wb * 4, hb * 4
            if (pw, ph) != (w, h):
                padded = Image.new('RGBA', (pw, ph))
                padded.paste(level.resize((pw, ph), Image.NEAREST))
                padded.paste(level, (0, 0))
                level = padded
            rp, bp = os.path.join(tmp, 'p%d.rgba' % m), os.path.join(tmp, 'b%d.bin' % m)
            open(rp, 'wb').write(level.tobytes())
            run('encode-bc7', rp, bp, pw, ph, 4)
            write_mip_blocks(t, m, open(bp, 'rb').read())
    rel = os.path.relpath(os.path.abspath(src), inside) if inside else os.path.join('Tex', os.path.basename(src))
    dst = os.path.join(mod, 'romfs', rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    zs(bytes(t.d), romfs, dct, dst, tmp0)
    tmp_obj.cleanup()
    print('%s: %d mip levels encoded as BC7 -> %s' % (os.path.basename(src), t.mips, dst))
    return dst


if __name__ == '__main__':
    try:
        if len(sys.argv) == 4 and sys.argv[1] == 'export':
            export_png(sys.argv[2], sys.argv[3])
        elif len(sys.argv) == 5 and sys.argv[1] == 'import':
            import_png(sys.argv[2], sys.argv[3], sys.argv[4])
        else:
            print(__doc__)
            sys.exit(2)
    except ToolError as e:
        raise SystemExit(str(e))
