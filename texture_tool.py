# SPDX-License-Identifier: GPL-3.0-or-later
"""Texture exporter / importer for Ocarina of Time (Switch 2) mods.

  python texture_tool.py export <romfs\\Tex\\name.bntx.zs> <out.png>
  python texture_tool.py export-dds <romfs\\Tex\\name.bntx.zs> <out.dds>
  python texture_tool.py import <romfs\\Tex\\name.bntx.zs> <edited.png or .dds> <mod folder>
  python texture_tool.py sizes <mods folder>

export:     the texture's full-size image (mip 0) as a PNG. ASTC and BC1..BC7 textures. BC4/BC5 SNORM data is shown shifted
            by 128 (0 = -1, 128 = 0, 255 = +1); BC4 as grey, BC5 as red and green.
export-dds: BC1..BC7 textures in their own format with every mip level, for paint programs that edit DDS.
import:     the PNG (same size as the texture) back into a copy of the texture under <mod folder>\\romfs\\Tex\\, every mip
            level made from it, for the menu's Mods tab. BC1, BC3, BC4, BC5 and BC7 keep their format (and the file its
            size); ASTC becomes BC7 and the game's resource size table is raised in its own mod. A .dds in the texture's
            format is taken as it is (its mip levels too; missing ones are made from its first level). BC2 and BC6H: export only.
sizes:      rebuilds the merged resource size table for all mods.

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
        self.snorm = (self.fmt & 0xff) == 0x02   # signed (BC4/BC5 SNORM; BC6H SF16)
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
            img = bc_image(t.kind, w, h, snorm_flip(t, blocks), t.snorm)   # BC4/BC5 SNORM shown shifted by 128
    return t, img


PILLOW_FOURCC = {0x1a: b'DXT1', 0x1b: b'DXT3', 0x1c: b'DXT5', 0x1d: b'ATI1', 0x1e: b'ATI2'}


def bc_image(kind, w, h, blocks, snorm=False):
    """BC1..BC7 blocks (row-major) as a Pillow RGBA image. sRGB data decodes to the same bytes as UNORM, so the UNORM
    form is used; Pillow reads BC1..BC5 only with the old FourCC header and BC6H/BC7 only with the DX10 one."""
    from PIL import Image
    if kind in PILLOW_FOURCC:
        hdr = b'DDS ' + struct.pack('<7I44x', 124, 0x81007, h, w, len(blocks), 0, 1)
        hdr += struct.pack('<2I4s20x', 32, 4, PILLOW_FOURCC[kind]) + struct.pack('<5I', 0x1000, 0, 0, 0, 0)
    else:
        dxgi = (96 if snorm else 95) if kind == 0x1f else 98
        hdr = b'DDS ' + struct.pack('<7I44x', 124, 0x81007, h, w, len(blocks), 0, 1) + struct.pack('<2I4s20x', 32, 4, b'DX10')
        hdr += struct.pack('<5I', 0x1000, 0, 0, 0, 0) + struct.pack('<5I', dxgi, 3, 0, 1, 0)
    return Image.open(io.BytesIO(hdr + blocks)).convert('RGBA')


def export_png(src, png):
    t, img = decode_mip0(src)
    img.save(png)
    print('%s: %s %dx%d, %d mip levels%s -> %s' % (os.path.basename(src), format_name(t), t.w, t.h, t.mips,
                                                   ' sRGB' if t.srgb else '', png))
    return t


def align(v, a):
    return (v + a - 1) // a * a


def to_bc7_layout(t):
    """An ASTC texture's container rebuilt for BC7: the same size and mip levels, BC7 (sRGB kept), each level laid out
    block-linear at 512-byte offsets; the data block, the relocation table after it and the header fields that point at
    them are moved to match. The new mip data is zeros (import_png fills it). Returns the new Texture."""
    d = t.d
    brtd = d.find(b'BRTD')
    old_brtd_size = struct.unpack_from('<Q', d, brtd + 8)[0]
    rlt = struct.unpack_from('<I', d, 0x18)[0]
    rlt_block = bytes(d[rlt:])
    head = bytearray(d[:t.data_start])
    t.kind, t.bw, t.bh, t.bpb = 0x20, 4, 4, 16
    offsets, pos = [], t.data_start
    for m in range(t.mips):
        w, h, wb, hb = t.mip_dims(m)
        g = t.gobs_y(m)
        size = ((wb * 16 + 63) // 64) * 512 * g * ((hb + 8 * g - 1) // (8 * g))
        offsets.append(pos)
        pos = align(pos + size, 512)
    data_size = pos - t.data_start
    new_rlt = align(t.data_start + data_size, 0x1000)
    new_brtd_size = new_rlt - brtd
    i = t.brti
    struct.pack_into('<I', head, i + 0x1C, (0x20 << 8) | (t.fmt & 0xff))
    struct.pack_into('<I', head, i + 0x50, data_size)
    ptrs = struct.unpack_from('<Q', head, i + 0x70)[0]
    for k, o in enumerate(offsets):
        struct.pack_into('<Q', head, ptrs + 8 * k, o)
    struct.pack_into('<Q', head, brtd + 8, new_brtd_size)
    struct.pack_into('<I', head, 0x18, new_rlt)
    struct.pack_into('<I', head, 0x1C, new_rlt + len(rlt_block))
    rb = bytearray(rlt_block)
    struct.pack_into('<I', rb, 4, new_rlt)
    for sec in range(struct.unpack_from('<I', rb, 8)[0]):
        at = 0x10 + sec * 0x18
        if struct.unpack_from('<I', rb, at + 8)[0] == brtd and struct.unpack_from('<I', rb, at + 12)[0] == old_brtd_size:
            struct.pack_into('<I', rb, at + 12, new_brtd_size)
    out = head + bytes(new_rlt - t.data_start) + rb
    nt = Texture(out)
    if nt.kind != 0x20 or nt.mip_offsets != offsets:
        raise ToolError('the BC7 layout could not be written')
    return nt


# ---- the game's resource size table (System/Resource/ResourceSizeTable...rsizetable.zs) ----
# "RESTBL", version, name length (160), CRC entry count, name entry count; then (CRC32 of the path without .zs, size) sorted
# by CRC, then (path, size) for paths whose CRC collides. The size a texture needs is its unpacked size + 280. A mod file
# bigger than its entry is not loaded (or the load hangs), so grown entries are merged into one table for all mods
# (the larger value per entry, as Hookshot's mod notes describe).
RSTB_PATH = os.path.join('System', 'Resource', 'ResourceSizeTable.Product.111.Nin_Ounce_NVN2.rsizetable.zs')
SIZE_MOD = '~Size table (made by the texture tool)'


def rstb_read(data):
    import zlib
    if data[:6] != b'RESTBL':
        raise ToolError('not a resource size table')
    ver, namelen, ncrc, nname = struct.unpack_from('<IIII', data, 6)
    crc = {}
    at = 22
    for _ in range(ncrc):
        c, v = struct.unpack_from('<II', data, at)
        crc[c] = v
        at += 8
    names = {}
    for _ in range(nname):
        n = data[at:at + namelen].split(b'\0', 1)[0].decode()
        names[n] = struct.unpack_from('<I', data, at + namelen)[0]
        at += namelen + 4
    return ver, namelen, crc, names


def rstb_write(ver, namelen, crc, names):
    out = bytearray(b'RESTBL' + struct.pack('<IIII', ver, namelen, len(crc), len(names)))
    for c in sorted(crc):
        out += struct.pack('<II', c, crc[c])
    for n in sorted(names):
        out += n.encode().ljust(namelen, b'\0') + struct.pack('<I', names[n])
    return bytes(out)


def rstb_set(crc, names, path, size):
    """Raises the entry of `path` (no .zs, '/' separators) to at least `size`. True when it changed."""
    import zlib
    if path in names:
        if names[path] >= size:
            return False
        names[path] = size
        return True
    c = zlib.crc32(path.encode())
    if crc.get(c, 0) >= size:
        return False
    crc[c] = size
    return True


def merge_sizes(mods_dir, romfs=None):
    """One size table for every mod in mods_dir: the game's table, each mod's own table (larger values win) and the
    unpacked size + 280 of each mod texture. Written as the mod SIZE_MOD (last in the load order by its name); removed
    when nothing needs a larger entry. Returns the number of entries raised."""
    romfs = romfs or game_romfs(None)
    with tempfile.TemporaryDirectory() as tmp:
        base, dict_index = unzs(os.path.join(romfs, RSTB_PATH), romfs, tmp)
        ver, namelen, crc, names = rstb_read(base)
        raised = 0
        for mod in sorted(os.listdir(mods_dir)):
            root = os.path.join(mods_dir, mod, 'romfs')
            if mod == SIZE_MOD or not os.path.isdir(root):
                continue
            own = os.path.join(root, RSTB_PATH)
            if os.path.exists(own):
                _, _, mcrc, mnames = rstb_read(unzs(own, romfs, tmp)[0])
                for c, v in mcrc.items():
                    if crc.get(c, 0) < v:
                        crc[c] = v
                        raised += 1
                for n, v in mnames.items():
                    if names.get(n, 0) < v:
                        names[n] = v
                        raised += 1
            for dirpath, _, files in os.walk(root):
                for f in files:
                    if not f.endswith('.bntx.zs'):
                        continue
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, root).replace(os.sep, '/')[:-3]
                    raised += rstb_set(crc, names, rel, len(unzs(full, romfs, tmp)[0]) + 280)
        out_dir = os.path.join(mods_dir, SIZE_MOD)
        dst = os.path.join(out_dir, 'romfs', RSTB_PATH)
        if raised:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            zs(rstb_write(ver, namelen, crc, names), romfs, dict_index, dst, tmp)
            open(os.path.join(out_dir, 'README.txt'), 'w').write(
                'Made by the texture tool: the game\'s resource size table with the entries the other mods need raised.\n'
                'Keep it on and last in the Mods tab. The tool rebuilds it after each import.\n')
        elif os.path.exists(dst):
            os.remove(dst)
    return raised


ENCODE = {0x1a: ('encode-bc1', 12), 0x1c: ('encode-bc3', 12), 0x1d: ('encode-bc4', 0), 0x1e: ('encode-bc5', 0),
          0x20: ('encode-bc7', 4)}   # texconv command and quality level per format the tool can write
_FLIP = bytes(range(128, 256)) + bytes(range(128))   # x ^ 0x80


def snorm_flip(t, blocks):
    """BC4/BC5 SNORM <-> UNORM blocks: the endpoint bytes XOR 0x80 (signed -128..127 <-> 0..255, same order and steps, so
    the same block mode). Exports show SNORM data shifted by 128 (0 = -1, 128 = 0, 255 = +1); imports undo it."""
    if not (t.snorm and t.kind in (0x1d, 0x1e)):
        return blocks
    b = bytearray(blocks)
    for o in ((0, 1) if t.kind == 0x1d else (0, 1, 8, 9)):
        b[o::t.bpb] = b[o::t.bpb].translate(_FLIP)
    return bytes(b)


DDS_FOURCC = {b'DXT1': 71, b'DXT2': 74, b'DXT3': 74, b'DXT4': 77, b'DXT5': 77, b'ATI1': 80, b'BC4U': 80, b'BC4S': 81,
              b'ATI2': 83, b'BC5U': 83, b'BC5S': 84}
DXGI_KIND = {70: 0x1a, 71: 0x1a, 72: 0x1a, 73: 0x1b, 74: 0x1b, 75: 0x1b, 76: 0x1c, 77: 0x1c, 78: 0x1c, 79: 0x1d, 80: 0x1d,
             81: 0x1d, 82: 0x1e, 83: 0x1e, 84: 0x1e, 94: 0x1f, 95: 0x1f, 96: 0x1f, 97: 0x20, 98: 0x20, 99: 0x20}
DXGI_NAME = {0x1a: 'BC1', 0x1b: 'BC2', 0x1c: 'BC3', 0x1d: 'BC4', 0x1e: 'BC5', 0x1f: 'BC6H', 0x20: 'BC7'}


def read_dds(path):
    """(dxgi format, width, height, [mip level block data, ...]) of a block-compressed .dds file."""
    d = open(path, 'rb').read()
    if d[:4] != b'DDS ' or len(d) < 128:
        raise ToolError('%s is not a DDS file' % os.path.basename(path))
    h, w = struct.unpack_from('<II', d, 12)
    mips = max(1, struct.unpack_from('<I', d, 28)[0])
    fourcc = d[84:88]
    if fourcc == b'DX10':
        dxgi, dim, _, arr = struct.unpack_from('<IIII', d, 128)
        off = 148
        if arr > 1:
            raise ToolError('%s is a texture array; save a single 2D texture' % os.path.basename(path))
    else:
        dxgi, off = DDS_FOURCC.get(fourcc), 128
    if dxgi not in DXGI_KIND:
        raise ToolError('%s is not saved as BC1 to BC7 (DXGI format %s). Save it as BC1, BC3, BC4, BC5 or BC7, or use a PNG.'
                        % (os.path.basename(path), dxgi if dxgi is not None else fourcc.decode('latin-1')))
    bpb = 8 if DXGI_KIND[dxgi] in (0x1a, 0x1d) else 16
    levels = []
    for m in range(mips):
        lw, lh = max(1, w >> m), max(1, h >> m)
        n = ((lw + 3) // 4) * ((lh + 3) // 4) * bpb
        if off + n > len(d):
            break
        levels.append(d[off:off + n])
        off += n
    return dxgi, w, h, levels


def dds_signed(dxgi):
    return dxgi in (81, 84)


def import_png(src, png, mod):
    """Import a PNG (or a .dds already in the texture's format) into the mod's copy of the texture. BC1, BC3, BC4, BC5 and
    BC7 keep their format and size; ASTC becomes BC7 (the size table is raised). Returns the written path."""
    from PIL import Image
    inside = find_romfs(src)
    romfs = game_romfs(inside)
    tmp_obj = tempfile.TemporaryDirectory()
    tmp0 = tmp_obj.name
    d, dct = unzs(src, romfs, tmp0)
    t = Texture(d)
    if t.kind in ASTC_BLOCKS:
        t = to_bc7_layout(t)   # ASTC: the mod's copy becomes BC7 (bigger, so the size table is raised below)
    elif t.kind not in ENCODE:
        raise ToolError('%s is %s: imports go into BC1, BC3, BC4, BC5, BC7 and ASTC textures (export works for all)' %
                        (os.path.basename(src), format_name(t)))
    given = []   # mip levels taken as they are from a .dds
    if png.lower().endswith('.dds'):
        dxgi, w, h, levels = read_dds(png)
        if DXGI_KIND[dxgi] != t.kind:
            raise ToolError('the DDS is %s; %s needs %s%s (or use a PNG)' % (
                DXGI_NAME[DXGI_KIND[dxgi]], os.path.basename(src), DXGI_NAME[t.kind],
                ' (the mod\'s copy of this ASTC texture is BC7)' if t.fmt != Texture(d).fmt else ''))
        if (w, h) != (t.w, t.h):
            raise ToolError('the DDS is %dx%d; the texture is %dx%d' % (w, h, t.w, t.h))
        flip = dds_signed(dxgi) != t.snorm and t.kind in (0x1d, 0x1e)
        given = [snorm_flip(t, lv) if flip else lv for lv in levels[:t.mips]]
        # the levels the DDS lacks are made from its first level (in the shifted form exports use for SNORM)
        img = bc_image(t.kind, t.w, t.h, snorm_flip(t, given[0]), t.snorm) if len(given) < t.mips else None
    else:
        img = Image.open(png).convert('RGBA')
        if img.size != (t.w, t.h):
            raise ToolError('the PNG is %dx%d; the texture is %dx%d' % (img.size[0], img.size[1], t.w, t.h))
    cmd, quality = ENCODE[t.kind]
    with tempfile.TemporaryDirectory() as tmp:
        for m in range(t.mips):
            if m < len(given):
                write_mip_blocks(t, m, given[m])
                continue
            w, h, wb, hb = t.mip_dims(m)
            level = img if m == 0 else img.resize((w, h), Image.LANCZOS)
            if t.kind == 0x1d:   # BC4: one channel, from the grey value
                g = level.convert('L')
                level = Image.merge('RGBA', (g, g, g, Image.new('L', g.size, 255)))
            # block formats work on 4x4 blocks: a level smaller than a block is padded (its edge repeated)
            pw, ph = wb * 4, hb * 4
            if (pw, ph) != (w, h):
                padded = Image.new('RGBA', (pw, ph))
                padded.paste(level.resize((pw, ph), Image.NEAREST))
                padded.paste(level, (0, 0))
                level = padded
            rp, bp = os.path.join(tmp, 'p%d.rgba' % m), os.path.join(tmp, 'b%d.bin' % m)
            open(rp, 'wb').write(level.tobytes())
            run(cmd, rp, bp, pw, ph, quality)
            write_mip_blocks(t, m, snorm_flip(t, open(bp, 'rb').read()))
    rel = os.path.relpath(os.path.abspath(src), inside) if inside else os.path.join('Tex', os.path.basename(src))
    dst = os.path.join(mod, 'romfs', rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    zs(bytes(t.d), romfs, dct, dst, tmp0)
    tmp_obj.cleanup()
    print('%s: %d mip levels as %s%s -> %s' % (os.path.basename(src), t.mips, format_name(t),
                                               ' (%d from the DDS)' % len(given) if given else '', dst))
    raised = merge_sizes(os.path.dirname(os.path.abspath(mod)), romfs)
    if raised:
        print('size table: %d entries raised in "%s" (keep that mod on and last)' % (raised, SIZE_MOD))
    return dst


def export_dds(src, dds):
    """The texture in its own format with every mip level, as a DDS (BC1 to BC7; ASTC has no common DDS form)."""
    romfs = game_romfs(find_romfs(src))
    with tempfile.TemporaryDirectory() as tmp:
        t = Texture(unzs(src, romfs, tmp)[0])
    if t.kind in ASTC_BLOCKS:
        raise ToolError('%s is %s: paint programs cannot open ASTC DDS files, so export it as a PNG' %
                        (os.path.basename(src), format_name(t)))
    dxgi = BC[t.kind][2] + (1 if t.srgb and t.kind in (0x1a, 0x1b, 0x1c, 0x20) else 0) + (1 if t.snorm else 0)
    if t.kind == 0x1f:
        dxgi = 96 if t.snorm else 95
    data = b''.join(read_mip_blocks(t, m) for m in range(t.mips))
    first = len(read_mip_blocks(t, 0))
    hdr = b'DDS ' + struct.pack('<7I44x', 124, 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000, t.h, t.w, first, 0, t.mips)
    hdr += struct.pack('<2I4s20x', 32, 4, b'DX10') + struct.pack('<5I', 0x1000 | 0x400000 | 0x8, 0, 0, 0, 0)
    hdr += struct.pack('<5I', dxgi, 3, 0, 1, 0)
    open(dds, 'wb').write(hdr + data)
    print('%s: %s %dx%d, %d mip levels -> %s' % (os.path.basename(src), format_name(t), t.w, t.h, t.mips, dds))
    return t

if __name__ == '__main__':
    try:
        if len(sys.argv) == 4 and sys.argv[1] == 'export':
            export_png(sys.argv[2], sys.argv[3])
        elif len(sys.argv) == 4 and sys.argv[1] == 'export-dds':
            export_dds(sys.argv[2], sys.argv[3])
        elif len(sys.argv) == 5 and sys.argv[1] == 'import':
            import_png(sys.argv[2], sys.argv[3], sys.argv[4])
        elif len(sys.argv) == 3 and sys.argv[1] == 'sizes':
            print('size table: %d entries raised' % merge_sizes(sys.argv[2]))
        else:
            print(__doc__)
            sys.exit(2)
    except ToolError as e:
        raise SystemExit(str(e))
