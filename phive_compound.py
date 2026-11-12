# SPDX-License-Identifier: GPL-3.0-or-later
"""Read a level's static collision (Phive/StaticCompoundBody/<area>/<level>_X*_Z*.bphsc, unpacked), for drawing the
terrain pieces ("MapDefault" physics) whose own mesh the model reader cannot draw and that have no shape of their own.

Found by reading the files (no Havok code used):
- A Phive header; u32 table at +0x10: [0] actor table, [1] (next table), [3] body table, [4] (its end). The file then
  holds several Havok tagfiles (TAG0); the root object of each (item 1) is an hknpCompoundShape.
- Actor table: 0x20-byte records (u32 first body, u32 last body, u64 actor hash (the layout's "Hash"), ...); 0xFFFFFFFF
  when the actor has no body here.
- Body table: 0x14-byte records; u16 at +0x0A is the instance number in one of the compounds. Which compound is not
  stored plainly, so it is found by matching instance positions against the actors' positions.
- hknpCompoundShape: instances at +0x38 (u32 offset from the field), count at +0x40. Each instance is 0x40 bytes:
  rotation quaternion (x, y, z, w), position float3 at +0x10, scale float3 at +0x20, shape at +0x30 (u64 offset from
  the field). Instance positions are in world space.
- Shapes: hknpMeshShape (see phive_shape), hknpConvexShape and its boxes and cylinders (vertices float3 at +0x38,
  faces at +0x48 as u16 first index, u8 count, u8; vertex indices u8 at +0x50: relative arrays), nested compounds.
  Capsules (two points and a radius) are left out.
"""
import struct

import numpy as np

import phive_shape


def _rel(d, field):
    off, n = struct.unpack_from('<2I', d, field)
    return field + off, n


def _tag_items(d, at):
    """(data start, [(type name, absolute offset, count)]) of the tagfile whose TAG0 header starts at at."""
    secs = phive_shape._sections(d, at, len(d), {})
    data = secs[b'DATA'][0]
    a, b = secs[b'TST1']
    strs = d[a:b].split(b'\0')
    names = [None]
    a, _ = secs[b'TNA1']
    n, o = _packed(d, a)
    for _ in range(1, n):
        ni, o = _packed(d, o)
        nt, o = _packed(d, o)
        for _ in range(nt):
            _, o = _packed(d, o)
            _, o = _packed(d, o)
        names.append(strs[ni].decode('latin1') if ni < len(strs) else '')
    a, b = secs[b'ITEM']
    items = []
    for o in range(a, b - 11, 12):
        t, off, cnt = struct.unpack_from('<3I', d, o)
        t &= 0xFFFFFF
        items.append((names[t] if 0 < t < len(names) else '', data + off, cnt))
    return data, items


def _packed(d, o):
    b = d[o]
    if b < 0x80:
        return b, o + 1
    if b < 0xC0:
        return ((b & 0x3F) << 8) | d[o + 1], o + 2
    if b < 0xE0:
        return ((b & 0x1F) << 16) | (d[o + 1] << 8) | d[o + 2], o + 3
    return ((b & 0x07) << 24) | (d[o + 1] << 16) | (d[o + 2] << 8) | d[o + 3], o + 4


def _quat(x, y, z, w):
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]], np.float64)


def _convex(d, o):
    vp, nv = _rel(d, o + 0x38)
    fp, nf = _rel(d, o + 0x48)
    ip, ni = _rel(d, o + 0x50)
    if not nv or not nf:
        return None
    pos = np.frombuffer(d, '<f4', nv * 3, vp).reshape(-1, 3).astype(np.float32)
    idx = d[ip:ip + ni]
    tris = []
    for k in range(nf):
        first, cnt = struct.unpack_from('<HB', d, fp + 4 * k)
        f = idx[first:first + cnt]
        tris += [(f[0], f[j], f[j + 1]) for j in range(1, len(f) - 1)]
    t = np.array(tris, np.int64).reshape(-1, 3)
    return pos, t[(t < nv).all(1)]


class Collision:
    """The static collision of one level part. shapes_for(hash, position) -> [(world positions, triangles)]."""

    def __init__(self, d):
        if d[:5] != b'Phive':
            raise ValueError('not a Phive file')
        self.d = d
        tab = struct.unpack_from('<5I', d, 0x10)
        self.actors = {}
        for o in range(tab[0], tab[1] - 0x1F, 0x20):
            lo, hi, h = struct.unpack_from('<IIQ', d, o)
            if lo != 0xFFFFFFFF and hi >= lo:
                self.actors[h] = (lo, hi)
        self.bodies = [struct.unpack_from('<H', d, o + 0x0A)[0] for o in range(tab[3], tab[4] - 0x13, 0x14)]
        self.roots, self.kinds = [], {}
        at = d.find(b'TAG0')
        while at >= 4:
            try:
                _, items = _tag_items(d, at - 4)
            except (KeyError, IndexError, struct.error):
                items = []
            for name, off, cnt in items:
                self.kinds[off] = name
            if len(items) > 1 and items[1][0] == 'hknpCompoundShape':
                self.roots.append(items[1][1])
            at = d.find(b'TAG0', at + 4)
        self._shape, self._inst = {}, {}

    def instances(self, comp):
        """[(rotation-scale 3x3, position, shape offset)] of a compound shape."""
        d = self.d
        arr = comp + 0x38 + struct.unpack_from('<I', d, comp + 0x38)[0]
        out = []
        for k in range(struct.unpack_from('<I', d, comp + 0x40)[0]):
            e = arr + 0x40 * k
            p = e + 0x30
            off = struct.unpack_from('<Q', d, p)[0]
            if not off:
                out.append(None)
                continue
            q = struct.unpack_from('<4f', d, e)
            m = _quat(*q) * np.array(struct.unpack_from('<3f', d, e + 0x20))[None, :]
            out.append((m, np.array(struct.unpack_from('<3f', d, e + 0x10)), p + off))
        return out

    def geometry(self, shape, m=None, t=None, depth=0):
        """[(positions, triangles)] of a shape, moved by m (3x3) and t."""
        kind = self.kinds.get(shape, '')
        if kind == 'hknpCompoundShape' and depth < 4:
            out = []
            for inst in self.instances(shape):
                if inst:
                    im, it, s = inst
                    out += self.geometry(s, im if m is None else m @ im, it if t is None else m @ it + t, depth + 1)
            return out
        if shape not in self._shape:
            try:
                if kind == 'hknpMeshShape':
                    self._shape[shape] = phive_shape.mesh_shape(self.d, shape)
                elif kind in ('hknpConvexShape', 'hknpBoxShape', 'hknpCylinderShape'):
                    self._shape[shape] = _convex(self.d, shape)
                else:
                    self._shape[shape] = None
            except (struct.error, ValueError, IndexError):
                self._shape[shape] = None
        g = self._shape[shape]
        if g is None:
            return []
        pos, tris = g
        if m is not None:
            pos = (pos @ m.T + t).astype(np.float32)
        return [(pos, tris)]

    def shapes_for(self, h, at):
        """The collision of the actor with layout hash h standing at position at, in world space."""
        if h not in self.actors:
            return []
        lo, hi = self.actors[h]
        at = np.asarray(at, np.float64)
        out = []
        for b in range(lo, min(hi, len(self.bodies) - 1) + 1):
            k = self.bodies[b]
            for root in self.roots:
                if root not in self._inst:
                    self._inst[root] = self.instances(root)
                insts = self._inst[root]
                if k < len(insts) and insts[k] is not None and np.abs(insts[k][1] - at).max() < 0.01:
                    m, t, s = insts[k]
                    out += self.geometry(s, m, t)
                    break
        return out
