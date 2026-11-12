# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the meshes of an Ocarina of Time (Switch 2) model (.bfres, version 10) for the texture tool's model view, and draw
them with a small software renderer (numpy). Only what the preview needs: the first model's shapes at full detail
(positions, UVs, normals, triangles), each shape's material and its texture names. No skinning or animation: skinned
meshes are drawn in their bind pose; rigid ones (skin count 0 or 1) are placed by their bone.

Layout (offsets measured on this game's files; field names after BfresLibrary / Switch-Toolbox):
  file  +0x28 models  +0xB0 buffer info (+0x08: start of the vertex/index data)  +0xD0 string table
  FMDL  +0x18 skeleton  +0x20 vertex buffers (0x58 each)  +0x28 shapes (0x60 each)  +0x38 materials (0xB0 each)
        +0x68 u16 counts: vertex buffers, shapes, materials
  FVTX  +0x08 attributes (0x10: name, u32 format, u16 offset, u16 buffer)  +0x30 buffer sizes (0x10 each)
        +0x38 strides (0x10 each)  +0x48 u32 data offset  +0x4C u8 attribute count  +0x4D u8 buffer count
        +0x50 u32 vertex count  +0x54 u8 skin count
  FSHP  +0x08 name  +0x18 meshes (0x38 each, LODs; the first is full detail)  +0x20 skin bone indices
        +0x52 u16 material  +0x54 u16 bone  +0x56 u16 vertex buffer  +0x5A u8 skin count  +0x5B u8 mesh count
  mesh  +0x20 u32 index data offset  +0x24 primitive (3 = triangles)  +0x28 index format (0 u8, 1 u16, 2 u32)
        +0x2C u32 index count  +0x30 u32 first vertex
  FMAT  +0x08 name  +0x20 texture names  +0xA3 u8 texture count
  FSKL  +0x10 bones (0x58 each: +0x00 name, +0x20 u16 index, +0x22 parent, +0x2C u32 flags (0x1000: euler XYZ, else quaternion),
        +0x30 scale, +0x3C rotation (quaternion or euler XYZ), +0x4C translation)  +0x38 u16 bone count
"""
import math
import struct

import numpy as np

# Switch vertex attribute formats: high byte = layout, low byte = type (1 unorm, 2 snorm, 3 uint, 4 sint, 5 float)
LAYOUTS = {0x02: (1, 8), 0x09: (2, 8), 0x0B: (4, 8), 0x0A: (1, 16), 0x12: (2, 16), 0x15: (4, 16), 0x14: (1, 32),
           0x17: (2, 32), 0x18: (3, 32), 0x19: (4, 32)}


class Mesh:
    def __init__(self, name, material, textures, pos, uv, nrm, tris):
        self.name, self.material, self.textures = name, material, textures
        self.pos, self.uv, self.nrm, self.tris = pos, uv, nrm, tris


class Model:
    def __init__(self, name, meshes):
        self.name, self.meshes = name, meshes

    def textures(self):
        return sorted({t for m in self.meshes for t in m.textures})


def _attr(b, base, count, stride, offset, fmt):
    layout, kind = fmt >> 8, fmt & 0xff
    if layout == 0x0E:   # 10_10_10_2
        v = np.ndarray((count,), '<u4', b, base + offset, (stride,)).astype(np.int64)
        comps = [(v >> s) & 0x3ff for s in (0, 10, 20)]
        if kind == 2:
            comps = [np.where(c >= 512, c - 1024, c) / 511.0 for c in comps]
        else:
            comps = [c / 1023.0 for c in comps]
        return np.stack(comps, 1).astype(np.float32)
    if layout not in LAYOUTS:
        return None
    n, bits = LAYOUTS[layout]
    dt = {(8, 1): 'u1', (8, 3): 'u1', (8, 2): 'i1', (8, 4): 'i1', (16, 1): '<u2', (16, 3): '<u2', (16, 2): '<i2',
          (16, 4): '<i2', (16, 5): '<f2', (32, 5): '<f4', (32, 3): '<u4', (32, 4): '<i4'}.get((bits, kind))
    if dt is None:
        return None
    a = np.ndarray((count, n), dt, b, base + offset, (stride, bits // 8)).astype(np.float32)
    if kind == 1:
        a /= float((1 << bits) - 1)
    elif kind == 2:
        a = np.maximum(a / float((1 << (bits - 1)) - 1), -1.0)
    return a


def _quat(x, y, z, w):
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]], np.float32)


def _euler(x, y, z):
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], np.float32)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], np.float32)
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], np.float32)
    return rz @ ry @ rx


def parse(b, name=''):
    """A Model from an unpacked .bfres file (bytes)."""
    u8 = lambda o: b[o]
    u16 = lambda o: struct.unpack_from('<H', b, o)[0]
    u32 = lambda o: struct.unpack_from('<I', b, o)[0]
    u64 = lambda o: struct.unpack_from('<Q', b, o)[0]

    def s(o):
        if not o or o + 2 > len(b):
            return ''
        return b[o + 2:o + 2 + u16(o)].decode('utf-8', 'replace')

    if b[:4] != b'FRES':
        raise ValueError('not a BFRES file')
    mdl = u64(0x28)
    if not mdl or b[mdl:mdl + 4] != b'FMDL':
        raise ValueError('no model in this file')
    data = u64(u64(0xB0) + 8)
    vtx, shp, mat, skl = u64(mdl + 0x20), u64(mdl + 0x28), u64(mdl + 0x38), u64(mdl + 0x18)
    nshp, nmat = u16(mdl + 0x6A), u16(mdl + 0x6C)

    mats = []
    for i in range(nmat):
        o = mat + i * 0xB0
        arr, n = u64(o + 0x20), u8(o + 0xA3)
        mats.append((s(u64(o + 8)), [s(u64(arr + 8 * k)) for k in range(n)] if arr else []))

    bones, m2b = [], []
    if skl and b[skl:skl + 4] == b'FSKL':
        bones = _bones(b, skl, u16, u32, u64, s)
        # matrix-to-bone list: smooth matrices, then rigid ones (+0x3A, +0x3C counts)
        lst, n = u64(skl + 0x18), u16(skl + 0x3A) + u16(skl + 0x3C)
        m2b = [u16(lst + 2 * k) for k in range(n)] if lst else []

    meshes = []
    for i in range(nshp):
        o = shp + i * 0x60
        if b[o:o + 4] != b'FSHP':
            continue
        sname = s(u64(o + 8))
        mi, bone, vi, skin = u16(o + 0x52), u16(o + 0x54), u16(o + 0x56), u8(o + 0x5A)

        v = vtx + vi * 0x58
        count, nattr, nbuf = u32(v + 0x50), u8(v + 0x4C), u8(v + 0x4D)
        sizes = [u32(u64(v + 0x30) + 0x10 * k) for k in range(nbuf)]
        strides = [u32(u64(v + 0x38) + 0x10 * k) for k in range(nbuf)]
        starts, at = [], data + u32(v + 0x48)
        for k in range(nbuf):
            starts.append(at)
            at += (sizes[k] + 7) & ~7
        attrs = {}
        for k in range(nattr):
            a = u64(v + 8) + k * 0x10
            an, fmt, off, buf = s(u64(a)), u32(a + 8), u16(a + 12), u16(a + 14)
            if buf < nbuf and an in ('_p0', '_u0', '_n0', '_i0'):
                attrs[an] = _attr(b, starts[buf], count, strides[buf], off, fmt)
        pos = attrs.get('_p0')
        if pos is None:
            continue
        pos = pos[:, :3].copy()
        uv = attrs.get('_u0')
        uv = uv[:, :2].copy() if uv is not None else np.zeros((count, 2), np.float32)
        nrm = attrs.get('_n0')
        nrm = nrm[:, :3].copy() if nrm is not None else None
        # rigid meshes: vertices are in their bone's space
        if bones and skin <= 1:
            if skin == 0:
                world = bones[bone] if bone < len(bones) else None
                if world is not None:
                    pos = pos @ world[0].T + world[1]
                    if nrm is not None:
                        nrm = nrm @ world[0].T
            elif attrs.get('_i0') is not None and m2b:
                # the index is a matrix number; the skeleton's matrix-to-bone list names the bone
                idx = attrs['_i0'][:, 0].astype(np.int64)
                for j in np.unique(idx):
                    bi = m2b[j] if j < len(m2b) else len(bones)
                    if bi >= len(bones):
                        continue
                    sel = idx == j
                    if sel.any():
                        r, t = bones[bi]
                        pos[sel] = pos[sel] @ r.T + t
                        if nrm is not None:
                            nrm[sel] = nrm[sel] @ r.T
        m0 = u64(o + 0x18)
        ioff, prim, ifmt, icount, first = u32(m0 + 0x20), u32(m0 + 0x24), u32(m0 + 0x28), u32(m0 + 0x2C), u32(m0 + 0x30)
        if prim != 3:
            continue
        dt = {0: 'u1', 1: '<u2', 2: '<u4'}.get(ifmt, '<u2')
        idx = np.frombuffer(b, dt, icount, data + ioff).astype(np.int64) + first
        idx = idx[:len(idx) // 3 * 3].reshape(-1, 3)
        idx = idx[(idx < count).all(1)]
        mname, mtex = mats[mi] if mi < len(mats) else ('', [])
        meshes.append(Mesh(sname, mname, mtex, pos, uv, nrm, idx))
    return Model(name or s(u64(mdl + 8)), meshes)


def _bones(b, skl, u16, u32, u64, s):
    """World (rotation, translation) of every bone, from the skeleton's local scale/rotation/translation."""
    arr, count = u64(skl + 0x10), u16(skl + 0x38)
    out, local, parents = [], [], []
    for k in range(count if arr else 0):
        o = arr + k * 0x58
        if o + 0x58 > len(b):
            break
        parent = struct.unpack_from('<h', b, o + 0x22)[0]
        flags = u32(o + 0x2C)
        sc = struct.unpack_from('<3f', b, o + 0x30)
        rot = struct.unpack_from('<4f', b, o + 0x3C)
        tr = struct.unpack_from('<3f', b, o + 0x4C)
        r = _euler(*rot[:3]) if flags & 0x1000 else _quat(*rot)   # rotation mode bit: 1 = euler XYZ, 0 = quaternion
        local.append((r * np.array(sc, np.float32), np.array(tr, np.float32)))
        parents.append(parent)
    for i in range(len(local)):
        r, t = local[i]
        p = parents[i]
        if 0 <= p < i:
            pr, pt = out[p]
            r, t = pr @ r, pr @ t + pt
        out.append((r, t))
    return out


# ---------------------------------------------------------------- drawing

def view_matrix(yaw, pitch):
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], np.float32)
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], np.float32)
    return rx @ ry


def render(model, textures, size=(512, 512), yaw=0.6, pitch=-0.25, highlight=None, zoom=1.0, bg=(46, 50, 56),
           pick=False, dim_others=True, frame=None, tint=None):
    """Draw the model. textures: {texture name: RGBA numpy array (h, w, 4) uint8}. Each mesh uses the highlighted
    texture if it lists it, else its first _Alb texture, else grey. Meshes without the highlighted texture are dimmed
    when highlight is given (and dim_others). Returns an RGB numpy array (h, w, 3); with pick, also (mesh, u, v)
    per-pixel arrays (h, w): the mesh index (-1: background) and the texture coordinates there, for picking and
    painting. Meshes with an .owner array (one number per triangle, as level_view makes) add a fourth buffer: the
    owner under each pixel. frame: (centre, radius) to look at instead of fitting the model (the Levels tab).
    tint: an (r, g, b) colour mixed into the highlighted meshes, so they stand out in a big scene."""
    W, H = size
    meshes = [m for m in model.meshes if len(m.tris)]
    if not meshes:
        empty_img = np.full((H, W, 3), bg, np.uint8)
        return (empty_img, (np.full((H, W), -1, np.int32), np.zeros((H, W), np.float32), np.zeros((H, W), np.float32))) if pick else empty_img
    # frame the meshes that use the highlighted texture (the whole model when none do)
    if frame is not None:
        centre, radius = np.asarray(frame[0], np.float32), float(frame[1]) or 1.0
    else:
        framed = [m for m in meshes if highlight in m.textures] or meshes
        allpos = np.concatenate([m.pos for m in framed])
        lo, hi = allpos.min(0), allpos.max(0)
        if framed is not meshes:   # a little of the surroundings too
            pad = (hi - lo).max() * 0.35
            lo, hi = lo - pad, hi + pad
        centre = (lo + hi) / 2
        radius = float(np.linalg.norm(hi - lo)) / 2 or 1.0
    R = view_matrix(yaw, pitch)
    scale = min(W, H) / (2.1 * radius) * zoom
    light = np.array([0.35, 0.55, 0.75], np.float32)
    light /= np.linalg.norm(light)

    zbuf = np.full(W * H, np.inf, np.float32)
    bufs = (np.full(W * H, -1, np.int32), np.zeros(W * H, np.float32), np.zeros(W * H, np.float32)) if pick else None
    if pick and any(getattr(m, 'owner', None) is not None for m in meshes):
        bufs += (np.full(W * H, -1, np.int32),)
    colour = np.zeros((W * H, 3), np.float32)
    for m in meshes:
        tex_name = highlight if highlight in m.textures else next((t for t in m.textures if t.endswith('_Alb')), None)
        tex = textures.get(tex_name) if tex_name else None
        if tex_name is None:
            continue   # no colour texture (clear eye covers, shadow casters): not drawn
        dim = dim_others and highlight is not None and highlight not in m.textures
        if tint is not None and highlight is not None and not dim:
            base = tex if tex is not None else np.full((1, 1, 4), 170, np.uint8)
            tex = base.copy()
            tex[..., :3] = (base[..., :3] * 0.55 + np.asarray(tint, np.float32) * 0.45).astype(np.uint8)
        p = (m.pos - centre) @ R.T
        sx = p[:, 0] * scale + W / 2
        sy = -p[:, 1] * scale + H / 2
        sz = -p[:, 2]
        vshade = None
        if m.nrm is not None and len(m.nrm) == len(m.pos):   # smooth shading from the mesh's own normals
            n = m.nrm @ R.T
            n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
            vshade = (0.35 + 0.65 * np.abs(n @ light)).astype(np.float32)
        _raster(m, sx, sy, sz, p, tex, dim, light, W, H, zbuf, colour, vshade, bufs, model.meshes.index(m))
    img = colour.reshape(H, W, 3)
    empty = np.isinf(zbuf).reshape(H, W)
    img[empty] = bg
    out = np.clip(img, 0, 255).astype(np.uint8)
    if pick:
        return out, tuple(a.reshape(H, W) for a in bufs)
    return out


def _raster(m, sx, sy, sz, p, tex, dim, light, W, H, zbuf, colour, vshade=None, bufs=None, mesh_no=0):
    t = m.tris
    x, y, z = sx[t], sy[t], sz[t]                       # (n, 3)
    # face normals for shading (two-sided)
    e1, e2 = p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]]
    fn = np.cross(e1, e2)
    fn /= np.linalg.norm(fn, axis=1, keepdims=True) + 1e-12
    shade = 0.35 + 0.65 * np.abs(fn @ light)
    x0, x1 = np.floor(x.min(1)).astype(np.int64), np.ceil(x.max(1)).astype(np.int64)
    y0, y1 = np.floor(y.min(1)).astype(np.int64), np.ceil(y.max(1)).astype(np.int64)
    # clip boxes to the screen, then group triangles by box width and height (steps of about 1.4x, so thin
    # triangles like hair strands are not tested against big square boxes)
    x0, y0 = np.maximum(x0, 0), np.maximum(y0, 0)
    x1, y1 = np.minimum(x1, W - 1), np.minimum(y1, H - 1)
    keep = (x1 >= x0) & (y1 >= y0)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    lw = np.minimum(np.searchsorted(STEPS, bw), len(STEPS) - 1)
    lh = np.minimum(np.searchsorted(STEPS, bh), len(STEPS) - 1)
    key = np.where(keep, lw * 64 + lh, -1)
    order = np.argsort(key, kind='stable')
    key_sorted = key[order]
    starts = np.flatnonzero(np.r_[True, key_sorted[1:] != key_sorted[:-1]])
    ends = np.r_[starts[1:], len(order)]
    for a, b in zip(starts, ends):
        kk = key_sorted[a]
        if kk < 0:
            continue
        kx, ky = int(STEPS[kk // 64]), int(STEPS[kk % 64])
        per = max(1, 300000 // (kx * ky))
        for c in range(a, b, per):
            ids = order[c:min(b, c + per)]
            _fill(ids, (kx, ky), x, y, z, x0, y0, m, t, tex, dim, shade, W, H, zbuf, colour, vshade, bufs, mesh_no)


STEPS = np.unique(np.round(1.4 ** np.arange(0, 26)).astype(np.int64))   # 1, 2, 3, 4, 5, 8, 11, 15, 21, 29, 40, ...


def _cutout(material):
    m = material.lower()
    return 'alphamask' in m or 'alpha' in m or 'hair' in m or 'fur' in m or 'eyelash' in m or 'leaf' in m or 'grass' in m


def _fill(ids, k, x, y, z, x0, y0, m, t, tex, dim, shade, W, H, zbuf, colour, vshade=None, bufs=None, mesh_no=0):
    """Fill the triangles ids (boxes up to k = (kx, ky) pixels). bufs: optional (mesh, u, v) per-pixel buffers for picking and
    painting. Hidden fragments are dropped before the texture is read; only the nearest per pixel is shaded."""
    f32 = np.float32
    kx, ky = k
    gx, gy = np.meshgrid(np.arange(kx, dtype=f32), np.arange(ky, dtype=f32))
    px = (x0[ids, None].astype(f32) + gx.reshape(1, -1)) + f32(0.5)       # (n, k*k) pixel centres
    py = (y0[ids, None].astype(f32) + gy.reshape(1, -1)) + f32(0.5)
    xs, ys = x[ids].astype(f32), y[ids].astype(f32)
    ax, ay, bx, by, cx, cy = xs[:, 0, None], ys[:, 0, None], xs[:, 1, None], ys[:, 1, None], xs[:, 2, None], ys[:, 2, None]
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    ok_area = np.abs(area) > 1e-9
    inv = np.where(ok_area, 1.0 / np.where(ok_area, area, 1.0), 0.0).astype(f32)
    w0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) * inv
    w1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) * inv
    w2 = 1.0 - w0 - w1
    inside = (w0 >= -1e-4) & (w1 >= -1e-4) & (w2 >= -1e-4) & ok_area & (px >= 0) & (px < W) & (py >= 0) & (py < H)
    ti, pi = np.nonzero(inside)
    if not len(ti):
        return
    tri = ids[ti]
    b0, b1 = w0[ti, pi], w1[ti, pi]
    b2 = 1.0 - b0 - b1
    pix = py[ti, pi].astype(np.int64) * W + px[ti, pi].astype(np.int64)
    zt = z[tri].astype(f32)
    depth = b0 * zt[:, 0] + b1 * zt[:, 1] + b2 * zt[:, 2]
    # early depth test against what earlier batches drew
    keep = depth < zbuf[pix]
    if not keep.all():
        tri, pix, depth, b0, b1, b2 = tri[keep], pix[keep], depth[keep], b0[keep], b1[keep], b2[keep]
    if not len(tri):
        return
    tv = t[tri]
    uv = m.uv
    u = b0 * uv[tv[:, 0], 0] + b1 * uv[tv[:, 1], 0] + b2 * uv[tv[:, 2], 0]
    v = b0 * uv[tv[:, 0], 1] + b1 * uv[tv[:, 1], 1] + b2 * uv[tv[:, 2], 1]
    if tex is not None and _cutout(m.material):   # cut-outs: alpha decides before the depth contest
        th, tw = tex.shape[:2]
        solid = tex[(np.mod(v, 1.0) * th).astype(np.int64) % th, (np.mod(u, 1.0) * tw).astype(np.int64) % tw, 3] >= 128
        if not solid.all():
            tri, pix, depth, b0, b1, b2, u, v = (a[solid] for a in (tri, pix, depth, b0, b1, b2, u, v))
            tv = t[tri]
    if not len(tri):
        return
    # nearest fragment per pixel: farthest first, so the nearest is written last
    order = np.argsort(-depth, kind='stable')
    tri, pix, depth, b0, b1, b2, u, v, tv = (a[order] for a in (tri, pix, depth, b0, b1, b2, u, v, tv))
    win = np.zeros(W * H, np.int64)
    win[pix] = np.arange(len(pix))
    sel = np.unique(win[pix])
    tri, pix, depth, b0, b1, b2, u, v, tv = (a[sel] for a in (tri, pix, depth, b0, b1, b2, u, v, tv))
    if tex is not None:
        th, tw = tex.shape[:2]
        rgb = tex[(np.mod(v, 1.0) * th).astype(np.int64) % th, (np.mod(u, 1.0) * tw).astype(np.int64) % tw, :3].astype(f32)
    else:
        rgb = np.full((len(tri), 3), 170.0, f32)
    if dim:
        rgb = rgb.mean(1, keepdims=True) * 0.55 + 40
    if vshade is not None:
        rgb = rgb * (b0 * vshade[tv[:, 0]] + b1 * vshade[tv[:, 1]] + b2 * vshade[tv[:, 2]])[:, None]
    else:
        rgb = rgb * shade[tri, None]
    zbuf[pix] = depth
    colour[pix] = rgb
    if bufs is not None:
        bufs[0][pix] = mesh_no
        bufs[1][pix] = u
        bufs[2][pix] = v
        if len(bufs) > 3 and getattr(m, 'owner', None) is not None:
            bufs[3][pix] = m.owner[tri]
