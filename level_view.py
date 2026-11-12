# SPDX-License-Identifier: GPL-3.0-or-later
"""Put a level together for the texture tool's Levels tab: every placed actor's model, moved, turned and scaled into
place, as one bfres_model.Model that bfres_model.render can draw.

Rows come from level_index.py: [actor, model, x, y, z, rx, ry, rz, sx, sy, sz, hash] (rotations in radians, applied X then
Y then Z). Terrain pieces stored as "taskmesh" and actors without a model are left out.

Meshes that share textures and material are merged (far fewer draw steps); each triangle remembers its row
(mesh.owner), so a click still finds the actor. light() makes a coarse copy for drawing while the view moves.
"""
import re

import numpy as np

import bfres_model

FAR = re.compile(r'Far(?![a-z])|Background|SkySphere')   # distant scenery: mountains, far-off towns, sky domes


def is_far(row):
    return bool(FAR.search(row[0]))


def placement(row):
    """(rotation 3x3, scale (3,), translation (3,)) for an index row."""
    rx, ry, rz = row[5:8]
    return bfres_model._euler(rx, ry, rz), np.array(row[8:11], np.float32), np.array(row[2:5], np.float32)


def build(name, rows, get_model, skip=None, world=None):
    """A Model of the whole level. get_model(model name) -> bfres_model.Model or None. skip(row) -> True leaves a row
    out. world: {row number: [(positions, triangles)]} already in place (the level's own collision for terrain), drawn
    plain grey instead of the row's model. The result has .rows (the index rows), .centre and .radius (for the camera)."""
    groups = {}
    for i, row in enumerate(rows):
        if not row[1] or (skip and skip(row)):
            continue
        if world and world.get(i):
            for pos, tris in world[i]:
                if len(tris):
                    groups.setdefault(((), bfres_model.COLLISION), []).append(
                        (pos.astype(np.float32), np.zeros((len(pos), 2), np.float32), np.zeros_like(pos, np.float32), tris, i))
            continue
        m = get_model(row[1])
        if m is None:
            continue
        r, s, t = placement(row)
        rs = (r * s[None, :]).astype(np.float32)                                # R @ diag(S)
        nm = (r @ np.diag(1.0 / np.where(s == 0, 1, s))).astype(np.float32)    # normals: R @ S^-1
        for mesh in m.meshes:
            if not len(mesh.tris):
                continue
            nrm = mesh.nrm @ nm.T if mesh.nrm is not None and len(mesh.nrm) == len(mesh.pos) else \
                np.zeros_like(mesh.pos)
            uv = mesh.uv if mesh.uv is not None and len(mesh.uv) == len(mesh.pos) else np.zeros((len(mesh.pos), 2), np.float32)
            groups.setdefault((tuple(mesh.textures), mesh.material), []).append(
                (mesh.pos @ rs.T + t, uv, nrm, mesh.tris, i))
    meshes = []
    for (texs, mat), parts in groups.items():
        meshes.append(_merge(texs, mat, parts))
    model = bfres_model.Model(name, meshes)
    model.rows = rows
    if meshes:   # frame where most actors stand, so outlying cliffs and stray pieces do not shrink the view
        at = np.array([rows[i][2:5] for i in sorted({int(o) for m in meshes for o in np.unique(m.owner)})], np.float32)
        lo, hi = np.percentile(at, 3, 0), np.percentile(at, 97, 0)
        model.centre, model.radius = (lo + hi) / 2, max(float(np.linalg.norm(hi - lo)) / 2, 5.0)
    else:
        model.centre, model.radius = np.zeros(3, np.float32), 1.0
    return model


def _merge(texs, mat, parts):
    off, P, U, N, T, O = 0, [], [], [], [], []
    for pos, uv, nrm, tris, row in parts:
        P.append(pos)
        U.append(uv)
        N.append(nrm)
        T.append(tris + off)
        O.append(np.full(len(tris), row, np.int32))
        off += len(pos)
    mesh = bfres_model.Mesh(texs[0] if texs else '', mat, list(texs), np.concatenate(P).astype(np.float32),
                            np.concatenate(U).astype(np.float32), np.concatenate(N).astype(np.float32),
                            np.concatenate(T).astype(np.int64))
    n = np.linalg.norm(mesh.nrm, axis=1, keepdims=True)
    mesh.nrm = np.where(n > 0, mesh.nrm / np.maximum(n, 1e-12), 0).astype(np.float32) if n.any() else None   # none: face shading
    mesh.owner = np.concatenate(O)
    return mesh


def light(model, cells=300):
    """A coarse copy for drawing while the view moves: vertices snapped to a grid of about cells steps across the level
    and merged (vertex clustering), triangles that collapse dropped."""
    step = 2 * model.radius / cells
    out = []
    for m in model.meshes:
        q = np.floor(m.pos / step).astype(np.int64)
        key = (q[:, 0] * 73856093) ^ (q[:, 1] * 19349663) ^ (q[:, 2] * 83492791)
        _, first, inv = np.unique(key, return_index=True, return_inverse=True)
        t = inv.reshape(-1)[m.tris]
        keep = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
        t = t[keep]
        if not len(t):
            continue
        st = np.sort(t, 1)
        _, uniq = np.unique(st[:, 0] * 1_000_003 ** 2 + st[:, 1] * 1_000_003 + st[:, 2], return_index=True)
        t = t[uniq]
        mesh = bfres_model.Mesh(m.name, m.material, m.textures, m.pos[first], m.uv[first],
                                None if m.nrm is None else m.nrm[first], t)
        mesh.owner = m.owner[keep][uniq]
        out.append(mesh)
    lm = bfres_model.Model(model.name, out)
    lm.rows, lm.centre, lm.radius = model.rows, model.centre, model.radius
    return lm


def stats(model):
    return len(model.meshes), sum(len(m.tris) for m in model.meshes)
