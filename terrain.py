# SPDX-License-Identifier: GPL-3.0-or-later
"""The field ground of a level, from its heightmaps (TerrainArc), for the texture tool's Levels tab.

Found by reading the files (no game code used):
- Which terrain a level uses: Banc/GameBancParam/<level>.game__banc__GameBancParam.bgyml, TerrainSceneName and
  TerrainScenePosition (following $parent). level_index.py records them.
- TerrainArc/<scene>.tscb: "TSCB"; world scale (float, metres per unit) at 0x10 and height scale at 0x14; the tile
  count at 0x38; at 0x78 a relative offset (from the field) to a table of relative offsets, one per tile. A tile
  starts with its centre x, z and edge length (floats, in units), and at +0x60 a relative offset to its name.
  Tiles form a quadtree; only some have a heightmap file.
- TerrainArc/<scene>/<tile>.hght: "TARB" header, then zstd (zs_helper.py unpacks it). Unpacked: a small header with
  relative offsets (from the field) and sizes of two blocks at 0x1C and 0x2C. The second block is the heightmap:
  257 x 257 float32 (0..1, times the height scale), stored block-linear like the game's textures (64-byte x 8-row
  GOBs, 16 GOBs per block). Rows run along z, columns along x, edge to edge across the tile.
Checked against Dodongo's Cavern's terrain collision: median difference 0.3 m.
"""
import struct

import numpy as np

SIZE = 257


def _rel(d, field):
    return field + struct.unpack_from('<I', d, field)[0]


def read_tscb(d):
    """(world scale, height scale, [(centre x, centre z, edge, name)]) of a .tscb file."""
    if d[:4] != b'TSCB':
        raise ValueError('not a TSCB file')
    scale, height = struct.unpack_from('<2f', d, 0x10)
    n = struct.unpack_from('<I', d, 0x38)[0]
    tab = _rel(d, 0x78)
    tiles = []
    for k in range(n):
        a = _rel(d, tab + 4 * k)
        x, z, s = struct.unpack_from('<3f', d, a)
        p = _rel(d, a + 0x60)
        tiles.append((x, z, s, d[p:p + 64].split(b'\0')[0].decode('ascii', 'replace')))
    return scale, height, tiles


_OFFS = None


def heights(u):
    """257 x 257 float32 heights (0..1) of an unpacked .hght file, rows along z."""
    global _OFFS
    if _OFFS is None:
        import texture_tool
        _OFFS = np.array(texture_tool.swizzle_offsets(SIZE, SIZE, 4, 16), np.int64)
    o = _rel(u, 0x2C)
    n = struct.unpack_from('<I', u, 0x30)[0]
    if n < _OFFS.max() + 4 or o + n > len(u):
        raise ValueError('unexpected heightmap block (%d bytes)' % n)
    b = np.frombuffer(u, np.uint8, n, o)
    return b[_OFFS[:, None] + np.arange(4)].reshape(SIZE, SIZE, 4).copy().view('<f4').reshape(SIZE, SIZE)


def ground(tiles, scale, height, origin=(0, 0, 0), samples=33, clip=None):
    """(positions, triangles, normals) of the ground: tiles is [(centre x, centre z, edge, heights 257x257)]. Each tile
    is sampled samples x samples; parts covered by a finer tile are left to that tile, and cells at height 0 (no ground)
    are left out. clip: (x0, z0, x1, z1) in world space keeps only the cells inside (the distant mountains are left out)."""
    step = (SIZE - 1) // (samples - 1)
    idx = np.arange(0, SIZE, step)
    n = len(idx)
    boxes = [(x - s / 2, z - s / 2, x + s / 2, z + s / 2, s) for x, z, s, h in tiles]
    P, T, N, base = [], [], [], 0
    ox, oy, oz = origin
    for x, z, s, h in tiles:
        g = h[np.ix_(idx, idx)].astype(np.float32)
        u = (x - s / 2) + s * idx / (SIZE - 1)                 # unit coordinates of the samples
        w = (z - s / 2) + s * idx / (SIZE - 1)
        X, Z = np.meshgrid(u, w)                              # rows along z
        pos = np.stack([X * scale + ox, g * height + oy, Z * scale + oz], -1).reshape(-1, 3).astype(np.float32)
        # cell corners and centres
        a = (np.arange(n - 1)[None, :] + np.arange(n - 1)[:, None] * n).reshape(-1)
        cx = ((X[:-1, :-1] + X[1:, 1:]) / 2).reshape(-1)
        cz = ((Z[:-1, :-1] + Z[1:, 1:]) / 2).reshape(-1)
        keep = (g[:-1, :-1] > 0).reshape(-1) & (g[1:, 1:] > 0).reshape(-1) & (g[:-1, 1:] > 0).reshape(-1) & \
            (g[1:, :-1] > 0).reshape(-1)
        for bx0, bz0, bx1, bz1, bs in boxes:
            if bs < s and bx0 < x + s / 2 and bx1 > x - s / 2 and bz0 < z + s / 2 and bz1 > z - s / 2:
                keep &= ~((cx > bx0) & (cx < bx1) & (cz > bz0) & (cz < bz1))
        if clip is not None:
            wx, wz = cx * scale + ox, cz * scale + oz
            keep &= (wx >= clip[0]) & (wx <= clip[2]) & (wz >= clip[1]) & (wz <= clip[3])
        a = a[keep]
        if not len(a):
            continue
        tri = np.concatenate([np.stack([a, a + n, a + 1], 1), np.stack([a + 1, a + n, a + n + 1], 1)])
        # normals from the height differences
        gy, gx = np.gradient(g * height, (s * scale) / (n - 1))
        nrm = np.stack([-gx, np.ones_like(gx), -gy], -1).reshape(-1, 3)
        nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
        P.append(pos)
        T.append(tri + base)
        N.append(nrm.astype(np.float32))
        base += len(pos)
    if not P:
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int64), np.zeros((0, 3), np.float32)
    return np.concatenate(P), np.concatenate(T).astype(np.int64), np.concatenate(N)
