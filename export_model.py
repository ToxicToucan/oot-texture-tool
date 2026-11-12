# SPDX-License-Identifier: GPL-3.0-or-later
"""Export an Ocarina of Time (Switch 2) model as Wavefront OBJ + MTL with its textures as PNG, for Blender and other 3D tools.

    py -3.12 export_model.py <model> <out folder> [--romfs <game romfs>] [--mods <mods folder>] [--all-textures] [--list <words>]

<model>        a model name as in romfs\\Model (without .bfres.zs), e.g. Link_Child; --list shows the names containing the given words
--romfs        the game's romfs (default: OOT_ROMFS, else C:\\Switch2\\oot-recomp-probe\\romfs)
--mods         the game's mods folder: an enabled mod's copy of a texture is exported instead of the game's (mods.ini order, a later mod wins)
--all-textures every texture the model names (normal, specular and other maps too), not only each material's colour texture

Writes <out>\\<model>.obj, <model>.mtl and one <texture>.png per colour texture (the material's *_Alb texture). One OBJ group per mesh,
named after the mesh, with its material. Meshes are in the model's bind pose: rigid parts are placed by their bones, skinned parts as
stored (no skeleton or animation is exported). UVs are flipped to OBJ's bottom-left origin.

Uses the texture tool's readers (bfres_model.py for the meshes, texture_tool.py for the .zs files and the texture decoder),
so it needs what that tool needs: Python 3.14+ with compression.zstd for the .zs files (PY_ZSTD, default "py -3.15"), numpy and Pillow,
and texconv.exe for the textures (TEXCONV_DIR).
"""
import argparse
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bfres_model  # noqa: E402
import texture_tool as tt  # noqa: E402


def colour_texture(mesh):
    return next((t for t in mesh.textures if t.endswith('_Alb')), mesh.textures[0] if mesh.textures else None)


def mod_texture(mods, name):
    """The winning enabled mod's copy of Tex/<name>.bntx.zs, or None."""
    if not mods or not os.path.isdir(mods):
        return None
    found = sorted(e for e in os.listdir(mods) if os.path.isdir(os.path.join(mods, e)))
    order, off = [], set()
    ini = os.path.join(mods, 'mods.ini')
    if os.path.exists(ini):
        for line in open(ini, encoding='utf-8-sig'):
            if '=' in line and line.lstrip()[:1] not in '#;':
                k, v = line.split('=', 1)
                names = [x.strip() for x in v.split('|') if x.strip()]
                if k.strip() == 'order':
                    order = names
                elif k.strip() == 'off':
                    off.update(names)
    names = [n for n in order if n in found] + [n for n in found if n not in order]
    for m in reversed(names):   # the last enabled mod in the order wins
        p = os.path.join(mods, m, 'romfs', 'Tex', name + '.bntx.zs')
        if m not in off and os.path.exists(p):
            return p
    return None


def write_obj(model, folder, tex_of_material):
    """<model>.obj and <model>.mtl; returns (vertices, triangles)."""
    base = model.name
    nv = nt = 0
    with open(os.path.join(folder, base + '.obj'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('# %s, exported from Ocarina of Time (Switch 2) by export_model.py\n' % base)
        f.write('mtllib %s.mtl\n' % base)
        for i, m in enumerate(model.meshes):
            f.write('o %s\n' % (m.name or 'mesh%d' % i))
            f.write('usemtl %s\n' % (m.material or 'default'))
            f.writelines('v %.6f %.6f %.6f\n' % tuple(p) for p in m.pos)
            f.writelines('vt %.6f %.6f\n' % (uv[0], 1.0 - uv[1]) for uv in m.uv)
            has_n = m.nrm is not None
            if has_n:
                f.writelines('vn %.6f %.6f %.6f\n' % tuple(n) for n in m.nrm)
            for t in m.tris:
                a, b, c = (int(x) + nv + 1 for x in t)
                f.write(('f %d/%d/%d %d/%d/%d %d/%d/%d\n' % (a, a, a, b, b, b, c, c, c)) if has_n else ('f %d/%d %d/%d %d/%d\n' % (a, a, b, b, c, c)))
            nv += len(m.pos)
            nt += len(m.tris)
    with open(os.path.join(folder, base + '.mtl'), 'w', encoding='utf-8', newline='\n') as f:
        done = set()
        for m in model.meshes:
            name = m.material or 'default'
            if name in done:
                continue
            done.add(name)
            f.write('newmtl %s\nKa 0 0 0\nKd 1 1 1\nKs 0 0 0\nd 1\nillum 1\n' % name)
            t = tex_of_material.get(name)
            if t:
                f.write('map_Kd %s.png\n' % t)
            f.write('\n')
    return nv, nt


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('model', nargs='?')
    ap.add_argument('out', nargs='?')
    ap.add_argument('--romfs')
    ap.add_argument('--mods')
    ap.add_argument('--all-textures', action='store_true')
    ap.add_argument('--list', nargs='*', metavar='WORD')
    a = ap.parse_args()
    romfs = tt.game_romfs(a.romfs)
    if a.list is not None:
        words = [w.lower() for w in a.list]
        names = sorted(f[:-9] for f in os.listdir(os.path.join(romfs, 'Model')) if f.endswith('.bfres.zs') and not f[:-9].endswith(('.anim', '.skel')))
        for n in names:
            if all(w in n.lower() for w in words):
                print(n)
        return 0
    if not a.model or not a.out:
        ap.error('give a model name and an output folder (or --list)')
    src = os.path.join(romfs, 'Model', a.model + '.bfres.zs')
    if not os.path.exists(src) and '.' not in a.model:   # "Player_Child" for the file Player_Child.Player_Child
        a.model = a.model + '.' + a.model
        src = os.path.join(romfs, 'Model', a.model + '.bfres.zs')
    if not os.path.exists(src):
        raise SystemExit('no model %s in %s (try --list %s)' % (a.model, os.path.join(romfs, 'Model'), a.model.split('_')[0]))
    with tempfile.TemporaryDirectory() as tmp:
        data, _ = tt.unzs(src, romfs, tmp)
    model = bfres_model.parse(data, a.model)
    if not model.meshes:
        raise SystemExit('%s has no meshes this exporter can read (terrain pieces are not supported)' % a.model)
    os.makedirs(a.out, exist_ok=True)

    def source(t):
        return mod_texture(a.mods, t) or os.path.join(romfs, 'Tex', t + '.bntx.zs')

    tex_of_material = {}
    for m in model.meshes:
        name = m.material or 'default'
        if name in tex_of_material:
            continue
        # the colour texture: *_Alb, else *_Gn0 (child Link's hair keeps its colour there and names an _Alb that does not exist)
        cands = [t for t in m.textures if t.endswith('_Alb')] + [t for t in m.textures if t.endswith('_Gn0')]
        t = next((c for c in cands if os.path.exists(source(c))), colour_texture(m))
        if t:
            tex_of_material[name] = t
    wanted = model.textures() if a.all_textures else sorted(set(tex_of_material.values()))
    exported, missing = [], []
    for t in wanted:
        p = source(t)
        if not os.path.exists(p):
            missing.append(t)
            continue
        try:
            tt.export_png(p, os.path.join(a.out, t + '.png'))
            exported.append(t)
        except Exception as e:  # noqa: BLE001 - reported, the model still exports
            missing.append('%s (%s)' % (t, e))
    # a material whose texture could not be written gets no map_Kd (it shows white instead of a broken link)
    tex_of_material = {k: v for k, v in tex_of_material.items() if v in exported}
    nv, nt = write_obj(model, a.out, tex_of_material)
    print('%s: %d meshes, %d vertices, %d triangles, %d textures -> %s' % (a.model, len(model.meshes), nv, nt, len(exported),
                                                                            os.path.join(a.out, a.model + '.obj')))
    if missing:
        print('textures not exported: ' + ', '.join(missing))
    return 0


if __name__ == '__main__':
    sys.exit(main())
