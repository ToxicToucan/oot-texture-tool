# SPDX-License-Identifier: GPL-3.0-or-later
"""Put an edited model back into the game: the vertex positions of an OBJ (as "Export OBJ..." wrote it, then moved
in Blender, ZBrush or another program) go into a copy of the model's .bfres, for a mod.

Same mesh only: every mesh must keep its vertex count and order (move points; don't add, delete, remesh or
subdivide). The file keeps its size, so the game's resource size table stays right. Normals of moved vertices follow
the change in shape (worked out from the triangles, added to the model's own normals); tangents, UVs, skin weights and the skeleton are kept as they are. Each shape's
bounding boxes are widened to hold the new shape, so the game does not cull it early.

    py -3.12 model_import.py <model> <edited.obj> <mod folder> [--romfs <game romfs>]

The OBJ's objects are matched to the model's meshes in order, else by name (Blender's ".001" endings ignored), else
the vertex list is split by the meshes' vertex counts (ZBrush writes one list).
"""
import os
import struct
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bfres_model  # noqa: E402

KEEP = 2e-5   # moved less than this (the OBJ's 6 decimals): the original bytes are kept


class ImportError_(Exception):
    pass


def read_obj(path):
    """[(object name, positions (n, 3))] in file order; vertices before any o/g line form an unnamed object."""
    blocks, name, cur = [], '', []
    for line in open(path, encoding='utf-8', errors='replace'):
        if line.startswith('v '):
            cur.append([float(x) for x in line.split()[1:4]])
        elif line.startswith(('o ', 'g ')):
            if cur:
                blocks.append((name, np.array(cur, np.float64)))
                cur = []
            name = line[2:].strip()
    if cur:
        blocks.append((name, np.array(cur, np.float64)))
    return blocks


def _base(name):
    n = name.strip()
    head, dot, tail = n.rpartition('.')
    return head if dot and tail.isdigit() and len(tail) == 3 else n


def match(meshes, blocks):
    """[positions per mesh] (world space, as exported) from the OBJ blocks, or raise ImportError_ with what differs."""
    counts = [len(m.pos) for m in meshes]
    have = [len(p) for _, p in blocks]
    if have == counts:
        return [p for _, p in blocks]
    by_name = {}
    for n, p in blocks:
        by_name.setdefault(_base(n), []).append(p)
    names = [m.name for m in meshes]
    if len(set(names)) == len(names) and all(len(by_name.get(n, [])) == 1 and len(by_name[n][0]) == c
                                             for n, c in zip(names, counts)):
        return [by_name[n][0] for n in names]
    allv = np.concatenate([p for _, p in blocks]) if blocks else np.zeros((0, 3))
    if len(allv) == sum(counts):
        return np.split(allv, np.cumsum(counts)[:-1])
    lines = ['%s: model %d, OBJ %s' % (m.name, c, next((str(len(p)) for n, p in blocks if _base(n) == m.name), 'missing'))
             for m, c in zip(meshes, counts)]
    raise ImportError_('The OBJ does not have the same vertices as the model (%d in the OBJ, %d in the model). Only moved '
                       'vertices can be imported: keep every vertex (no remesh, subdivide, delete or merge).\n%s'
                       % (len(allv), sum(counts), '\n'.join(lines[:30])))


def _half(a):
    return np.asarray(a, np.float32).astype('<f2')


def _write_pos(buf, at, stride, fmt, count, pos):
    if fmt == 0x1805:     # float32 x 3
        for k in range(3):
            np.ndarray((count,), '<f4', buf, at + 4 * k, (stride,))[:] = pos[:, k]
    elif fmt == 0x1505:   # float16 x 4 (w kept)
        for k in range(3):
            np.ndarray((count,), '<f2', buf, at + 2 * k, (stride,))[:] = _half(pos[:, k])
    else:
        raise ImportError_('positions stored as format 0x%x, which this importer cannot write yet' % fmt)


def _write_nrm(buf, at, stride, fmt, count, nrm, sel):
    if fmt != 0x0E02:     # 10_10_10_2 signed
        return False
    v = np.ndarray((count,), '<u4', buf, at, (stride,))
    q = np.clip(np.round(nrm * 511.0), -511, 511).astype(np.int64) & 0x3FF
    new = (v.astype(np.int64) & ~0x3FFFFFFF) | q[:, 0] | (q[:, 1] << 10) | (q[:, 2] << 20)
    v[sel] = new[sel].astype(np.uint32)
    return True


def _normals(pos, tris):
    n = np.zeros_like(pos)
    f = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
    for k in range(3):
        np.add.at(n, tris[:, k], f)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    return np.where(ln > 0, n / np.maximum(ln, 1e-20), 0)


def apply(data, model, new_world):
    """data: the unpacked .bfres; model: bfres_model.parse(data); new_world: positions per mesh as exported (world
    space). Returns (new bytes, moved vertex count)."""
    buf = bytearray(data)
    moved_total = 0
    shapes = {}
    for m, new in zip(model.meshes, new_world):
        new = np.asarray(new, np.float64)
        old = m.pos.astype(np.float64)
        moved = np.abs(new - old).max(1) > KEEP
        if not moved.any():
            continue
        src = m.src
        if src is None or '_p0' not in src['attrs']:
            raise ImportError_('%s: no position data to write' % m.name)
        local = new.copy()
        for sel, r, t in src['place']:   # undo the bone placement of rigid meshes
            inv = np.linalg.inv(np.asarray(r, np.float64))
            idx = slice(None) if sel is None else sel
            local[idx] = (new[idx] - t) @ inv.T
        at, stride, fmt = src['attrs']['_p0']
        count = src['count']
        stored = bfres_model._attr(buf, at - 0, count, stride, 0, fmt)[:, :3].astype(np.float64)
        stored[moved] = local[moved]
        _write_pos(buf, at, stride, fmt, count, stored)
        if '_n0' in src['attrs'] and len(m.tris):
            nrm = _normals(new, m.tris)
            if m.nrm is not None and len(m.nrm) == len(nrm):
                # keep the model's own normals (hard edges, hand-tuned shading) and add only the change in shape
                before = _normals(old, m.tris)
                if float(np.sum(before * m.nrm)) < 0:   # the mesh's winding: point the way its own normals do
                    nrm, before = -nrm, -before
                nrm = m.nrm.astype(np.float64) + (nrm - before)
            # back into the stored space (rotation part only)
            for sel, r, t in src['place']:
                inv = np.linalg.inv(np.asarray(r, np.float64))
                idx = slice(None) if sel is None else sel
                nrm[idx] = nrm[idx] @ inv.T
            ln = np.linalg.norm(nrm, axis=1, keepdims=True)
            nrm = np.where(ln > 0, nrm / np.maximum(ln, 1e-20), 0)
            sel = moved & (np.linalg.norm(nrm, axis=1) > 0)
            a, st, f = src['attrs']['_n0']
            _write_nrm(buf, a, st, f, count, nrm, sel)
        moved_total += int(moved.sum())
        lo, hi = shapes.get(src['shape'], (np.full(3, np.inf), np.full(3, -np.inf)))
        shapes[src['shape']] = (np.minimum(lo, stored.min(0)), np.maximum(hi, stored.max(0)))
    for o, (lo, hi) in shapes.items():
        _widen_bounds(buf, o, lo, hi)
    return bytes(buf), moved_total


def _widen_bounds(buf, shape, lo, hi):
    """Widen the shape's bounding boxes (centre, half size; at FSHP +0x38, up to the array at +0x40) to hold lo..hi."""
    u64 = lambda o: struct.unpack_from('<Q', buf, o)[0]
    a, b = u64(shape + 0x38), u64(shape + 0x40)
    if not a or not b or b <= a or (b - a) % 24 or (b - a) // 24 > 64:
        return
    for k in range((b - a) // 24):
        c = np.array(struct.unpack_from('<3f', buf, a + 24 * k), np.float64)
        e = np.array(struct.unpack_from('<3f', buf, a + 24 * k + 12), np.float64)
        nlo, nhi = np.minimum(c - e, lo), np.maximum(c + e, hi)
        struct.pack_into('<6f', buf, a + 24 * k, *((nlo + nhi) / 2), *((nhi - nlo) / 2))


def import_obj(model_name, obj, mod, romfs, source=None):
    """Write <mod>/romfs/Model/<model>.bfres.zs from the OBJ. source: the .bfres.zs to start from (default the game's).
    Returns a one-line summary."""
    import texture_tool as tt
    src = source or os.path.join(romfs, 'Model', model_name + '.bfres.zs')
    with tempfile.TemporaryDirectory() as tmp:
        data, dict_index = tt.unzs(src, romfs, tmp)
        model = bfres_model.parse(data, model_name)
        if not model.meshes:
            raise ImportError_('%s has no meshes this importer can write (terrain pieces are not supported)' % model_name)
        new_world = match(model.meshes, read_obj(obj))
        out, moved = apply(data, model, new_world)
        if len(out) != len(data):
            raise ImportError_('internal: the file size changed')
        dst = os.path.join(mod, 'romfs', 'Model', model_name + '.bfres.zs')
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tt.zs(out, romfs, dict_index, dst, tmp)
    return '%s: %d of %d vertices moved -> %s' % (model_name, moved, sum(len(m.pos) for m in model.meshes), dst)


def main():
    import argparse
    import texture_tool as tt
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('model')
    ap.add_argument('obj')
    ap.add_argument('mod')
    ap.add_argument('--romfs')
    a = ap.parse_args()
    try:
        print(import_obj(a.model, a.obj, a.mod, tt.game_romfs(a.romfs)))
    except ImportError_ as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
