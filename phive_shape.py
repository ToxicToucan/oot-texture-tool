# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the triangles of a collision shape (Phive/Shape/Dcc/*.bphsh, unpacked), for drawing terrain the model reader cannot
(the "taskmesh" models). The file is a Phive header and a Havok tagfile (TAG0) holding an hknpMeshShape.

Found by reading the files (no Havok code used):
- TAG0 > DATA holds the objects; INDX > ITEM lists them: u32 (flags << 24 | type), u32 offset in DATA, u32 count.
- The mesh shape (the first item with count 1) keeps its vertex scale at +0x40 (float3, e.g. 512) and its inverse at +0x50.
- Geometry sections are the item whose entries are 0x40 bytes and point at the other arrays. Each section:
  +0x00 BVH nodes, +0x08 primitives (u8 a, b, c, d: a triangle when c == d, else the quad a b c d),
  +0x10 vertex bytes (u16 x, y, z each), +0x18 materials: relative arrays (u32 offset from the field, u32 count);
  +0x20 int3 section offset. A vertex is (section offset + (x, y, z)) / scale.
"""
import struct

import numpy as np


def _sections(d, o, end, out):
    while o + 8 <= end:
        hdr = struct.unpack_from('>I', d, o)[0]
        size, flag = hdr & 0x3FFFFFFF, hdr >> 30
        if size < 8 or o + size > end:
            break
        tag = d[o + 4:o + 8]
        out[tag] = (o + 8, o + size)
        if flag == 0:
            _sections(d, o + 8, o + size, out)
        o += size
    return out


def _rel(d, field):
    off, n = struct.unpack_from('<2I', d, field)
    return field + off, n


def read(d):
    """(positions float32 (n, 3), triangles int64 (m, 3)) of every geometry section, in the shape's own space."""
    tag = d.find(b'TAG0')
    if d[:5] != b'Phive' or tag < 4:
        raise ValueError('not a Phive shape')
    secs = _sections(d, tag - 4, len(d), {})
    if b'DATA' not in secs or b'ITEM' not in secs:
        raise ValueError('no DATA or ITEM in the tagfile')
    data = secs[b'DATA'][0]
    a, b = secs[b'ITEM']
    items = [struct.unpack_from('<3I', d, o) for o in range(a, b - 11, 12)]
    shape = next((data + off for t, off, n in items if n == 1 and t >> 24), None)
    if shape is None:
        raise ValueError('no shape object')
    scale = struct.unpack_from('<3f', d, shape + 0x40)
    if not all(s > 0 for s in scale):
        raise ValueError('unexpected vertex scale %r' % (scale,))
    starts = sorted(data + off for t, off, n in items)
    # the section array: entries of 0x40 bytes whose first three relative arrays land on other items
    found = None
    for t, off, n in items:
        if not n or not t >> 24:
            continue
        o = data + off
        ok = True
        for k in range(min(n, 3)):
            for f in (0x00, 0x08, 0x10):
                p, cnt = _rel(d, o + 0x40 * k + f)
                if cnt and p not in starts:
                    ok = False
        if ok and n and _rel(d, o + 0x10)[1] and _rel(d, o + 0x08)[1]:
            found = (o, n)
            break
    if found is None:
        raise ValueError('no geometry sections found')
    o, n = found
    pos, tris, base = [], [], 0
    for k in range(n):
        s = o + 0x40 * k
        pp, np_ = _rel(d, s + 0x08)
        vp, nv = _rel(d, s + 0x10)
        offset = np.array(struct.unpack_from('<3i', d, s + 0x20), np.float32)
        prim = np.frombuffer(d, np.uint8, np_ * 4, pp).reshape(-1, 4).astype(np.int64)
        nverts = nv // 6
        q = np.frombuffer(d, '<u2', nverts * 3, vp).reshape(-1, 3).astype(np.float32)
        pos.append((q + offset) / np.array(scale, np.float32))
        tri = prim[:, :3]
        quad = prim[prim[:, 2] != prim[:, 3]]
        t = np.concatenate([tri, quad[:, [0, 2, 3]]]) if len(quad) else tri
        t = t[(t < nverts).all(1)]
        tris.append(t + base)
        base += nverts
    return np.concatenate(pos), np.concatenate(tris)
