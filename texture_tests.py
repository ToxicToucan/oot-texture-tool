# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression checks for the texture tool. Writes only to a temporary folder (never to the game or a real mods folder).

  py -3.12 texture_tests.py [game romfs]        (default C:\\Switch2\\oot-recomp-probe\\romfs, or OOT_ROMFS)

Checks: for one texture of each format, export -> import unchanged -> export again stays close (PSNR floor per format),
alpha survives, and DDS export -> DDS import gives identical blocks; BC2/BC6H are refused; mods.ini is read like the
game; a few models load and draw. Needs texconv.exe (TEXCONV_DIR or next to texture_tool.py) and Python 3.14+ for zstd.
Exit code 0 when everything passes.
"""
import math
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import texture_tool as tt  # noqa: E402

ROMFS = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('OOT_ROMFS', r'C:\Switch2\oot-recomp-probe\romfs')
os.environ['OOT_ROMFS'] = ROMFS

# texture, lowest acceptable PSNR (dB) for the round trip
CASES = [('Cloth_LonLon_FlagFence_B_01_Alb', 55), ('CmnTex_Chara_Npc_Kokiri_WC_002_Flowers_Alb.00', 55),
         ('Cloth_Zora_Carpet_C_Alb', 40), ('Bomb_Leaf_Trs', 45), ('Grain_Rock_00_Nrm', 50), ('Smoke_Thin_DryIce_01_Dst', 60),
         ('Mt_Hairs_Player_Adult_Alb', 50), ('Player_Adult_Hair_Alb', 60)]
REFUSED = ['Etc_InsideGanonCastle_WindowGlass_Hdr']      # BC6H
MODELS = ['Weapon_Sword_Kokiri.Weapon_Sword_Kokiri', 'Animal_Cucco.Animal_Cucco', 'Player_Adult.Player_Adult']

failures = []


def check(ok, what):
    print(('PASS  ' if ok else 'FAIL  ') + what, flush=True)
    if not ok:
        failures.append(what)


def psnr(a, b):
    from PIL import ImageChops
    a, b = a.convert('RGBA'), b.convert('RGBA')
    h = ImageChops.difference(a, b).histogram()
    mse = sum(c * c * h[i * 256 + c] for i in range(4) for c in range(256)) / (a.width * a.height * 4)
    return 10 * math.log10(255 ** 2 / mse) if mse else 99.0


def blocks(path):
    with tempfile.TemporaryDirectory() as tmp:
        t = tt.Texture(tt.unzs(path, ROMFS, tmp)[0])
    return [tt.read_mip_blocks(t, m) for m in range(t.mips)]


def textures(work):
    mod = os.path.join(work, 'mods', 'Test mod')
    for name, floor in CASES:
        src = os.path.join(ROMFS, 'Tex', name + '.bntx.zs')
        if not os.path.exists(src):
            check(False, '%s: missing from the romfs' % name)
            continue
        t, img = tt.decode_mip0(src)
        png = os.path.join(work, name + '.png')
        img.save(png)
        t0 = time.time()
        dst = tt.import_png(src, png, mod)
        t2, img2 = tt.decode_mip0(dst)
        p = psnr(img, img2)
        check(p >= floor and img2.getextrema()[3] == img.getextrema()[3],
              '%s (%s): PNG round trip %.1f dB (floor %d), alpha %s, %.1fs' % (
                  name, tt.format_name(t), p, floor, img2.getextrema()[3], time.time() - t0))
        if t.kind not in tt.ASTC_BLOCKS:
            dds = os.path.join(work, name + '.dds')
            tt.export_dds(src, dds)
            dst = tt.import_png(src, dds, mod)
            check(blocks(src) == blocks(dst), '%s: DDS export + import gives identical blocks' % name)
    for name in REFUSED:
        src = os.path.join(ROMFS, 'Tex', name + '.bntx.zs')
        png = os.path.join(work, name + '.png')
        tt.export_png(src, png)
        try:
            tt.import_png(src, png, mod)
            check(False, '%s: import should be refused' % name)
        except tt.ToolError:
            check(True, '%s: import refused' % name)


def mods_ini(work):
    """The window's reading of mods.ini against the game's rules."""
    import importlib.machinery
    import importlib.util
    ld = importlib.machinery.SourceFileLoader('texture_gui', os.path.join(HERE, 'texture_gui.pyw'))
    g = importlib.util.module_from_spec(importlib.util.spec_from_loader('texture_gui', ld))
    ld.exec_module(g)
    mods = os.path.join(work, 'ordermods')
    tex = 'Mt_Hairs_Player_Adult_Alb.bntx.zs'
    for m in ('A, with comma', 'B', 'C new', 'D off'):
        os.makedirs(os.path.join(mods, m, 'romfs', 'Tex'))
        open(os.path.join(mods, m, 'romfs', 'Tex', tex), 'w').close()
    open(os.path.join(mods, 'mods.ini'), 'w').write('# a comment = here\norder = B | A, with comma | D off\noff = D off\n')
    be = g.Backend(dict(g.DEFAULTS, romfs=ROMFS, mods=mods))
    order, off = be.mod_order()
    check(order == ['B', 'A, with comma', 'D off', 'C new'] and off == {'D off'}, 'mods.ini order and off: %s %s' % (order, off))
    win = [v[0] for v in be.versions(tex) if v[2]]
    check(win == ['Mod: C new (on, shown in game)'], 'mods.ini: the copy the game shows: %s' % win)


def models():
    try:
        import numpy  # noqa: F401
    except ImportError:
        check(False, 'models: numpy is not installed')
        return
    import bfres_model
    for name in MODELS:
        with tempfile.TemporaryDirectory() as tmp:
            data, _ = tt.unzs(os.path.join(ROMFS, 'Model', name + '.bfres.zs'), ROMFS, tmp)
        m = bfres_model.parse(data, name)
        img = bfres_model.render(m, {}, (128, 128))
        drawn = int((img != img[0, 0]).any(2).sum())
        check(len(m.meshes) > 0 and drawn > 200, '%s: %d meshes, %d triangles, %d pixels drawn' % (
            name, len(m.meshes), sum(len(x.tris) for x in m.meshes), drawn))


def main():
    work = tempfile.mkdtemp(prefix='oot-texture-tests-')
    try:
        textures(work)
        mods_ini(work)
        models()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print('\n%d failed' % len(failures) if failures else '\nall passed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
