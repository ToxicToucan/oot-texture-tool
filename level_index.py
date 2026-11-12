# SPDX-License-Identifier: GPL-3.0-or-later
"""The game's levels for the texture tool's Levels tab. Python 3.14+ (compression.zstd).

  level_index.py <game romfs> <out.json> [only]      (only: just the levels whose name contains this, for tests)

Reads every level layout (Banc/<area>/<level>_Static/_Dynamic.bcett.byml.zs) and, for every actor placed there, its
actor pack (Pack/Actor/<actor>.pack.zs) to find the model it shows (ActorParam -> ModelInfo, following $parent).
Writes {"romfs": ..., "levels": {"Area/Level": [[actor, model or "", x, y, z, rx, ry, rz, sx, sy, sz, hash], ...]},
"models": {actor: model}, "shapes": {actor: [collision shape, ...]}, "terrain": {"Area/Level": [terrain scene, x, y, z]}}
(shapes: PhysicsParam -> ControllerSetParam -> ShapeParam -> Phive/Shape/Dcc/<shape>.bphsh.zs, drawn for terrain whose
own mesh cannot be read; terrain: Banc/GameBancParam, the heightmaps under TerrainArc). Rotations are radians (X, Y, Z). About a minute.
"""
import json
import os
import struct
import sys
from compression import zstd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import byml  # noqa: E402
import sarc  # noqa: E402


def sarc_files(data):
    hdr_len = struct.unpack_from('<H', data, 4)[0]
    data_off = struct.unpack_from('<I', data, 12)[0]
    count = struct.unpack_from('<H', data, hdr_len + 6)[0]
    nodes = [struct.unpack_from('<IIII', data, hdr_len + 12 + i * 16) for i in range(count)]
    return [data[data_off + s:data_off + e] for h, a, s, e in nodes]


class Reader:
    def __init__(self, romfs):
        self.romfs = romfs
        self.dicts = {}
        for raw in sarc_files(zstd.decompress(open(os.path.join(romfs, 'Pack', 'ZsDic.pack.zs'), 'rb').read())):
            d = zstd.ZstdDict(raw)
            self.dicts[d.dict_id] = d

    def read(self, *parts):
        raw = open(os.path.join(self.romfs, *parts), 'rb').read()
        d = self.dicts.get(zstd.get_frame_info(raw).dictionary_id)
        return zstd.decompress(raw, zstd_dict=d) if d else zstd.decompress(raw)


def ref_path(ref):
    """'?Component/ModelInfo/X.engine__component__ModelInfo.bgyml' or 'Work/.../X.gyml' -> the path inside the pack."""
    p = ref.lstrip('?')
    if p.startswith('Work/'):
        p = p[5:]
    if p.endswith('.gyml'):
        p = p[:-5] + '.bgyml'
    return p


def level_terrain(romfs, level):
    """[terrain scene, x, y, z] of a level (GameBancParam TerrainSceneName and TerrainScenePosition, following
    $parent), or None."""
    name, pos, seen = None, None, set()
    p = os.path.join(romfs, 'Banc', 'GameBancParam', level + '.game__banc__GameBancParam.bgyml')
    while p and p not in seen and os.path.exists(p):
        seen.add(p)
        try:
            b = byml.parse(open(p, 'rb').read()) or {}
        except Exception:  # noqa: BLE001 - unreadable settings: no terrain
            break
        if name is None and 'TerrainSceneName' in b:
            name = b['TerrainSceneName']
        if pos is None and isinstance(b.get('TerrainScenePosition'), dict):
            pos = [float(b['TerrainScenePosition'].get(k, 0.0)) for k in 'XYZ']
        parent = b.get('$parent')
        p = os.path.join(romfs, *ref_path(parent).split('/')) if parent else None
    if not name:
        return None
    return [name] + (pos or [0.0, 0.0, 0.0])


def actor_model(rd, actor, models):
    """('Project.Fmdb' or '', [collision shape names]) for an actor."""
    try:
        files = sarc.parse(rd.read('Pack', 'Actor', actor + '.pack.zs'))
    except (OSError, ValueError, zstd.ZstdError):
        return '', []

    def chain(path):
        seen = []
        while path in files and path not in seen and len(seen) < 16:
            seen.append(path)
            try:
                d = byml.parse(files[path])
            except Exception:
                break
            yield d
            par = d.get('$parent') if isinstance(d, dict) else None
            if not par:
                break
            path = ref_path(par)

    info_ref = phys_ref = None
    for d in chain('Actor/%s.engine__actor__ActorParam.bgyml' % actor):
        comps = d.get('Components') or {}
        info_ref = info_ref or comps.get('ModelInfoRef')
        phys_ref = phys_ref or comps.get('PhysicsRef')
    shapes = actor_shapes(files, chain, phys_ref)
    if info_ref:
        cands = [ref_path(info_ref)]
    else:   # no ActorParam link: a ModelInfo named after the actor, or the only one
        mi = [k for k in files if k.startswith('Component/ModelInfo/')]
        named = [k for k in mi if k.split('/')[-1].split('.')[0] == actor]
        cands = named or (mi if len(mi) == 1 else [])
    for c in cands:
        proj = fmdb = None
        for d in chain(c):
            proj = proj or d.get('ModelProjectName')
            fmdb = fmdb or d.get('FmdbName')
        if proj and fmdb and proj + '.' + fmdb in models:
            return proj + '.' + fmdb, shapes
    return '', shapes


def actor_shapes(files, chain, phys_ref):
    """Collision shapes (Phive/Shape/Dcc/<name>.bphsh.zs): PhysicsParam -> ControllerSetParam -> ShapeParam -> PhshMesh."""
    if not phys_ref:
        return []
    cset = None
    for d in chain(ref_path(phys_ref)):
        cset = cset or d.get('ControllerSetPath')
    if not cset:
        return []
    params = []
    for d in chain(ref_path(cset)):
        for e in d.get('ShapeNamePathAry') or []:
            if isinstance(e, dict) and e.get('FilePath') and ref_path(e['FilePath']) not in params:
                params.append(ref_path(e['FilePath']))
    out = []
    for sp in params:
        for d in chain(sp):
            for m in d.get('PhshMesh') or []:
                name = (m.get('PhshMeshPath') or '').split('/')[-1]
                if name.endswith('.phsh') and name[:-5] not in out:
                    out.append(name[:-5])
            if d.get('PhshMesh'):
                break
    return out


def vec(a, key, default):
    v = a.get(key)
    if isinstance(v, list) and len(v) == 3:
        return [round(float(x), 4) for x in v]
    if isinstance(v, dict):
        return [round(float(v.get(k, default)), 4) for k in 'XYZ']
    if isinstance(v, (int, float)):
        return [round(float(v), 4)] * 3
    return [default] * 3


def main(romfs, out, only=None):
    rd = Reader(romfs)
    models = {f[:-9] for f in os.listdir(os.path.join(romfs, 'Model')) if f.endswith('.bfres.zs')}
    banc = os.path.join(romfs, 'Banc')
    levels, actor_models, actor_shapes_, terrain = {}, {}, {}, {}
    files = [(area, f) for area in sorted(os.listdir(banc)) if os.path.isdir(os.path.join(banc, area))
             for f in sorted(os.listdir(os.path.join(banc, area))) if f.endswith('.bcett.byml.zs')]
    for i, (area, f) in enumerate(files):
        level = f[:-len('.bcett.byml.zs')]
        for part in ('_Static', '_Dynamic'):
            if level.endswith(part):
                level = level[:-len(part)]
        if only and only not in area + '/' + level:
            continue
        try:
            root = byml.parse(rd.read('Banc', area, f))
        except Exception as e:
            print('skipped %s/%s: %s' % (area, f, e), flush=True)
            continue
        rows = levels.setdefault(area + '/' + level, [])
        if area + '/' + level not in terrain:
            terrain[area + '/' + level] = level_terrain(romfs, level)
        for a in (root or {}).get('Actors') or []:
            name = a.get('Gyaml')
            if not name:
                continue
            if name not in actor_models:
                actor_models[name], shp = actor_model(rd, name, models)
                if shp:
                    actor_shapes_[name] = shp
            rows.append([name, actor_models[name]] + vec(a, 'Translate', 0.0) + vec(a, 'Rotate', 0.0) + vec(a, 'Scale', 1.0)
                        + [int(a.get('Hash') or 0)])
        if i % 10 == 0:
            print('%d/%d levels files, %d actors' % (i, len(files), len(actor_models)), flush=True)
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump({'version': 3, 'romfs': os.path.abspath(romfs), 'levels': levels, 'models': actor_models, 'shapes': actor_shapes_,
                   'terrain': {k: v for k, v in terrain.items() if v}}, fp, separators=(',', ':'))
    os.replace(tmp, out)
    print('done: %d levels, %d actors, %d with a model' % (len(levels), len(actor_models),
                                                         sum(1 for v in actor_models.values() if v)))


if __name__ == '__main__':
    main(*sys.argv[1:4])
