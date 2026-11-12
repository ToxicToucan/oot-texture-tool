# SPDX-License-Identifier: GPL-3.0-or-later
"""Which models use which textures, for the texture tool's model view. Python 3.14+ (compression.zstd).

  model_index.py <game romfs> <out.json>

Reads every Model/*.bfres.zs (animations and skeletons skipped) and lists the names in its string table that are also
textures in Tex/. Writes {"romfs": ..., "models": N, "index": {model: [texture, ...]}, "no_meshes": [model, ...]}
(no_meshes: models whose meshes the preview cannot read, the "taskmesh" terrain pieces). About 30 seconds.
"""
import json
import os
import re
import struct
import sys
from compression import zstd

RUN = re.compile(rb'[!-~]{3,}')


def sarc_files(data):
    hdr_len = struct.unpack_from('<H', data, 4)[0]
    data_off = struct.unpack_from('<I', data, 12)[0]
    count = struct.unpack_from('<H', data, hdr_len + 6)[0]
    nodes = [struct.unpack_from('<IIII', data, hdr_len + 12 + i * 16) for i in range(count)]
    return [data[data_off + s:data_off + e] for h, a, s, e in nodes]


def readable(b):
    """True when the first model has a shape with ordinary vertex attributes (not a "taskmesh" terrain piece)."""
    mdl = struct.unpack_from('<Q', b, 0x28)[0]
    if not mdl or b[mdl:mdl + 4] != b'FMDL':
        return False
    vtx = struct.unpack_from('<Q', b, mdl + 0x20)[0]
    n = struct.unpack_from('<H', b, mdl + 0x68)[0]
    return any(b[vtx + i * 0x58 + 0x4C] > 0 for i in range(n)) if vtx else False


def main(romfs, out):
    dicts = {}
    for raw in sarc_files(zstd.decompress(open(os.path.join(romfs, 'Pack', 'ZsDic.pack.zs'), 'rb').read())):
        d = zstd.ZstdDict(raw)
        dicts[d.dict_id] = d
    tex = {f[:-8] for f in os.listdir(os.path.join(romfs, 'Tex')) if f.endswith('.bntx.zs')}
    models = sorted(f for f in os.listdir(os.path.join(romfs, 'Model'))
                    if f.endswith('.bfres.zs') and '.anim.' not in f and '.Skeleton.' not in f)
    index, flat = {}, []
    for i, f in enumerate(models):
        try:
            raw = open(os.path.join(romfs, 'Model', f), 'rb').read()
            d = dicts.get(zstd.get_frame_info(raw).dictionary_id)
            b = zstd.decompress(raw, zstd_dict=d) if d else zstd.decompress(raw)
            off, size = struct.unpack_from('<QI', b, 0xD0)
            used = sorted({m.group().decode('ascii') for m in RUN.finditer(b, off, min(len(b), off + size + 16))} & tex)
            drawable = readable(b)
        except Exception:
            continue
        if used:
            index[f[:-9]] = used
            if not drawable:
                flat.append(f[:-9])
        if i % 250 == 0:
            print('%d/%d' % (i, len(models)), flush=True)
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump({'romfs': os.path.abspath(romfs), 'models': len(models), 'index': index, 'no_meshes': flat}, fp)
    os.replace(tmp, out)
    print('done %d models, %d with textures' % (len(models), len(index)))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
