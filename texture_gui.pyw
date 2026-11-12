#! python3.12
# SPDX-License-Identifier: GPL-3.0-or-later
"""A window for texture_tool.py: find a game texture, preview it, export it as a PNG, import an edited PNG into a mod.

Double-click "Texture Tool.cmd" (next to this file), or run:  py -3.12 texture_gui.pyw
The folders (game romfs, mods, texconv) are set in Settings and kept in %LOCALAPPDATA%\\oot-texture-gui.json.
It never writes to the game's romfs, merged\\bin or merged\\work: imports go only to <mods>\\<mod name>\\romfs\\Tex\\.
"""
import io
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import texture_tool as tt  # noqa: E402

# texture_tool reports problems with SystemExit, or ToolError in newer versions
TOOL_ERRORS = (SystemExit,) + ((tt.ToolError,) if hasattr(tt, "ToolError") else ())
ASTC_IMPORT = hasattr(tt, 'to_bc7_layout')          # ASTC textures are imported as BC7
SIZE_MOD = getattr(tt, 'SIZE_MOD', None)            # the mod holding the merged resource size table
ENCODABLE = set(getattr(tt, 'ENCODE', {0x20: None}))   # formats imports keep (BC7, and BC1/BC3/BC4/BC5 in newer tools)
ENCODABLE_NAMES = ', '.join(tt.BC[k][0] for k in sorted(ENCODABLE)) + (' and ASTC' if ASTC_IMPORT else '')

CONFIG = os.path.join(os.environ.get('LOCALAPPDATA', HERE), 'oot-texture-gui.json')
DEFAULTS = {
    'romfs': r'C:\Switch2\oot-recomp-probe\romfs',
    'mods': r'C:\Switch2\oot-recomp-probe\merged\mods',
    'texconv_dir': os.environ.get('TEXCONV_DIR', HERE),
    'py_zstd': os.environ.get('PY_ZSTD', 'py -3.15'),
    'last_dir': os.path.expanduser('~'),
}
PREVIEW = 512
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0   # CREATE_NO_WINDOW: no console flashes for the helper programs


class ToolError(Exception):
    """An error with a message meant for the owner."""


def load_config(path=CONFIG):
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding='utf-8') as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


MODS_OVERRIDE = None   # --mods on the command line: used for this run only, not saved


def save_config(cfg, path=CONFIG):
    if MODS_OVERRIDE is not None:
        cfg = dict(cfg, mods=load_config(path)['mods'])
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def time_now():
    import time
    return time.strftime('%I:%M:%S %p').lstrip('0')


def norm(p):
    return os.path.normcase(os.path.abspath(p))


def inside(path, folder):
    path, folder = norm(path), norm(folder)
    return path == folder or path.startswith(folder.rstrip('\\/') + os.sep)


class Backend:
    """Everything the window does, without the window (the round-trip test drives this directly)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.apply()

    def apply(self):
        tt.TEXCONV = os.path.join(self.cfg['texconv_dir'], 'texconv.exe')
        tt.PY_ZSTD = self.cfg['py_zstd'].split()
        os.environ['OOT_ROMFS'] = self.cfg['romfs']
        self._patch_subprocess()

    @staticmethod
    def _patch_subprocess():
        # texture_tool calls subprocess.run; under pythonw each call would flash a console window. Hide them.
        if getattr(tt.subprocess, '_gui_patched', False) or not NO_WINDOW:
            return
        real = subprocess.run

        def run(*a, **k):
            k.setdefault('creationflags', NO_WINDOW)
            return real(*a, **k)
        tt.subprocess = type(sys)('subprocess_gui')
        tt.subprocess.__dict__.update(subprocess.__dict__)
        tt.subprocess.run = run
        tt.subprocess._gui_patched = True

    @property
    def tex_dir(self):
        return os.path.join(self.cfg['romfs'], 'Tex')

    def check_tools(self):
        """A list of problems (empty when everything is in place)."""
        bad = []
        if not os.path.isdir(self.tex_dir):
            bad.append('The game textures folder was not found:\n  %s\nSet the game romfs folder in Settings.' % self.tex_dir)
        if not os.path.exists(os.path.join(self.cfg['romfs'], 'Pack', 'ZsDic.pack.zs')):
            bad.append('The game romfs has no Pack\\ZsDic.pack.zs (the zstd dictionaries):\n  %s' % self.cfg['romfs'])
        if not os.path.exists(tt.TEXCONV):
            bad.append('texconv.exe was not found in:\n  %s\nBuild it with build-texconv.cmd (see README.md), or set its folder in Settings.'
                       % self.cfg['texconv_dir'])
        for f in ('zs_helper.py', 'texture_tool.py'):
            if not os.path.exists(os.path.join(HERE, f)):
                bad.append('%s is missing next to this program (%s).' % (f, HERE))
        try:
            r = subprocess.run(tt.PY_ZSTD + ['-c', 'import compression.zstd'], capture_output=True, text=True,
                               creationflags=NO_WINDOW, timeout=60)
            ok = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if not ok:
            bad.append('"%s" did not run Python 3.14+ with compression.zstd (needed for the .zs files).\n'
                       'Install Python 3.14 or later, or set the command in Settings.' % self.cfg['py_zstd'])
        try:
            from PIL import Image, ImageTk  # noqa: F401
        except ImportError:
            bad.append('Pillow (with ImageTk) is not installed for this Python (%s).' % sys.executable)
        return bad

    def textures(self):
        try:
            return sorted((f for f in os.listdir(self.tex_dir) if f.endswith('.bntx.zs')), key=str.lower)
        except OSError:
            return []

    def path(self, name):
        return os.path.join(self.tex_dir, name)

    def decode(self, name, path=None):
        """(info dict, mip 0 as a PIL RGBA image) for a texture: the game's copy, or a mod's copy at path."""
        try:
            t, img = tt.decode_mip0(path or self.path(name))
        except TOOL_ERRORS as e:
            raise ToolError(str(e))
        astc = t.kind in tt.ASTC_BLOCKS
        importable = astc and ASTC_IMPORT or t.kind in ENCODABLE
        info = {'name': name, 'w': t.w, 'h': t.h, 'mips': t.mips, 'srgb': t.srgb, 'snorm': getattr(t, 'snorm', False),
                'format': tt.format_name(t), 'importable': importable, 'astc': astc, 'kind': t.kind,
                'dds': not astc}
        if not importable:
            info['why_not'] = ('ASTC textures export fine but this version cannot import them.' if astc else
                               '%s textures cannot be imported; only %s can.' % (info['format'], ENCODABLE_NAMES))
        return info, img

    def export(self, name, out, path=None):
        """PNG (mip 0) or .dds (every mip level, own format) of the game's copy or a mod's copy."""
        try:
            if out.lower().endswith('.dds'):
                tt.export_dds(path or self.path(name), out)
            else:
                tt.export_png(path or self.path(name), out)
        except TOOL_ERRORS as e:
            raise ToolError(str(e))

    def nx2navi_ini(self):
        """The nx2navi.ini of the NX2navi install whose mods folder this is (the mods folder next to nx2navi.exe), or None."""
        parent = os.path.dirname(os.path.abspath(self.cfg['mods']))
        ini = os.path.join(parent, 'nx2navi.ini')
        if os.path.basename(os.path.abspath(self.cfg['mods'])).lower() == 'mods' and (
                os.path.exists(ini) or os.path.exists(os.path.join(parent, 'nx2navi.exe'))):
            return ini
        return None

    @property
    def host(self):
        return 'NX2navi' if self.nx2navi_ini() else 'this recomp'

    def mod_order(self):
        """(load order, off set), later mods winning, as the game that uses this mods folder reads them.
        This recomp (runtime/src/services/mods.cpp): mods.ini "order = a | b", "off = x | y"; the listed mods, then the
        other folders by name; later wins. NX2navi (its mods folder: the one next to nx2navi.exe): nx2navi.ini keys
        mods_order and mods_disabled ('|' between names, keys case-insensitive, an optional NX2NAVI_ prefix, the
        environment first); the TOP mod wins, unlisted folders come after the listed ones by name. Returned reversed so
        that, here too, the last mod in the list wins. Every mod is on unless switched off."""
        mods_dir = self.cfg['mods']
        found = sorted(os.listdir(mods_dir)) if os.path.isdir(mods_dir) else []
        found = [m for m in found if os.path.isdir(os.path.join(mods_dir, m))]
        ini = self.nx2navi_ini()
        order, off = [], set()
        if ini:
            vals = {}
            try:
                for line in open(ini, encoding='utf-8', errors='replace'):
                    line = line.strip()
                    if not line or line[0] in '#;[' or '=' not in line:
                        continue
                    k, _, v = line.partition('=')
                    k = k.strip().upper()
                    k = k[8:] if k.startswith('NX2NAVI_') else k
                    vals[k] = v.strip().strip('"')
            except OSError:
                pass
            for k in ('MODS_ORDER', 'MODS_DISABLED'):
                if os.environ.get('NX2NAVI_' + k):
                    vals[k] = os.environ['NX2NAVI_' + k]
            order = [x.strip() for x in vals.get('MODS_ORDER', '').split('|') if x.strip()]
            off = {x.strip() for x in vals.get('MODS_DISABLED', '').split('|') if x.strip()}
            top_first = [m for m in dict.fromkeys(order) if m in found] + [m for m in found if m not in order]
            return list(reversed(top_first)), off
        try:
            for line in open(os.path.join(mods_dir, 'mods.ini'), encoding='utf-8'):
                if '=' not in line or line[:1] in '#;':
                    continue
                k, _, v = line.partition('=')
                items = [x.strip() for x in v.split('|') if x.strip()]
                if k.strip() == 'order':
                    order = items
                elif k.strip() == 'off':
                    off = set(items)
        except OSError:
            pass
        names = [m for m in order if m in found]
        names = list(dict.fromkeys(names)) + [m for m in found if m not in names]
        return names, off

    def modded(self):
        """{texture file name: [mod, ...]} for every texture some mod replaces."""
        out = {}
        for mod in self.mods():
            d = os.path.join(self.cfg['mods'], mod, 'romfs', 'Tex')
            try:
                for f in os.listdir(d):
                    if f.endswith('.bntx.zs'):
                        out.setdefault(f, []).append(mod)
            except OSError:
                pass
        return out

    def versions(self, name):
        """[(label, path or None, is_winner)]: the game's copy, then each mod's copy in load order. The winner is the
        last mod in the load order that is on (what the game shows)."""
        order, off = self.mod_order()
        mods = [m for m in order if m != SIZE_MOD and os.path.exists(os.path.join(self.cfg['mods'], m, 'romfs', 'Tex', name))]
        on = [m for m in mods if m not in off]
        winner = on[-1] if on else None
        out = [('Original (game)', None, winner is None)]
        for m in mods:
            state = 'off' if m in off else 'on'
            label = 'Mod: %s (%s%s)' % (m, state, ', shown in game' if m == winner else '')
            out.append((label, os.path.join(self.cfg['mods'], m, 'romfs', 'Tex', name), m == winner))
        return out

    # --- models (the model view)
    @property
    def index_path(self):
        d = os.path.join(os.environ.get('LOCALAPPDATA', HERE), 'oot-texture-gui')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'model_index.json')

    def model_index(self):
        """{model: [texture, ...]} from the cache, or None when it is missing or for another romfs."""
        if getattr(self, '_index', None) is not None:
            return self._index
        try:
            j = json.load(open(self.index_path, encoding='utf-8'))
            if norm(j['romfs']) != norm(self.cfg['romfs']) or 'no_meshes' not in j:
                return None   # another romfs, or an older list: rebuilt
            self._index = j['index']
            self._flat = set(j['no_meshes'])
            users = {}
            for m, ts in self._index.items():
                for t in ts:
                    users.setdefault(t, []).append(m)
            self._users = users
            return self._index
        except (OSError, ValueError, KeyError):
            return None

    def build_model_index(self):
        script = os.path.join(HERE, 'model_index.py')
        if not os.path.exists(script):
            raise ToolError('model_index.py is missing next to this program.')
        r = subprocess.run(tt.PY_ZSTD + ['-I', script, self.cfg['romfs'], self.index_path], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
        if r.returncode:
            raise ToolError('Building the model list failed:\n' + (r.stderr or r.stdout).strip()[-800:])
        self._index = None
        if self.model_index() is None:
            raise ToolError('The model list was not written.')
        return len(self._index)

    def models_for(self, name):
        """Models that use a texture, best guess first: the model named in the texture's name, then fewer textures."""
        if self.model_index() is None:
            return None
        tex = self.model_slot(name)
        users = self._users.get(tex, [])
        words = set(tex.lower().split('_'))

        def score(m):
            base = m.split('.')[0]
            named = base.lower() in tex.lower()
            shared = len(words & set(base.lower().split('_')))
            return (m in self._flat, not named, -shared, len(self._index.get(m, [])), m.lower())
        return sorted(users, key=score)

    def model_slot(self, name):
        """The texture name models use for this texture. Numbered variants (X_Alb.0 .. X_Alb.13, swapped in by the
        game) are all named X_Alb.0 in the model, so .N shows in the .0 slot."""
        tex = name[:-8] if name.endswith('.bntx.zs') else name
        if self.model_index() is not None and tex not in self._users:
            base, dot, num = tex.rpartition('.')
            if dot and num.isdigit() and base + '.0' in self._users:
                return base + '.0'
        return tex

    def load_model(self, model):
        import bfres_model
        cache = getattr(self, '_models', None)
        if cache is None:
            cache = self._models = {}
        if model in cache:
            return cache[model]
        src = os.path.join(self.cfg['romfs'], 'Model', model + '.bfres.zs')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data, _ = tt.unzs(src, tt.game_romfs(None), tmp)
            m = bfres_model.parse(data, model)
        except TOOL_ERRORS as e:
            raise ToolError(str(e))
        except Exception as e:
            raise ToolError('Could not read the model %s: %s' % (model, e))
        if len(cache) > 6:
            cache.pop(next(iter(cache)))
        cache[model] = m
        return m

    def model_textures(self, model, skip=()):
        """{texture: RGBA numpy array} for each mesh's colour texture, from the copy the game would load (the winning
        mod's, else the game's own), at a small mip level; cached by file and time."""
        import numpy as np
        cache = getattr(self, '_tex_small', None)
        if cache is None:
            cache = self._tex_small = {}
        order, off = self.mod_order()
        live = [m for m in reversed(order) if m not in off and m != SIZE_MOD]
        out = {}
        for m in model.meshes:
            t = next((x for x in m.textures if x.endswith('_Alb')), None)
            if not t or t in skip or t in out:
                continue
            p = next((q for q in (os.path.join(self.cfg['mods'], mod, 'romfs', 'Tex', t + '.bntx.zs') for mod in live)
                      if os.path.exists(q)), self.path(t + '.bntx.zs'))
            if not os.path.exists(p):
                continue
            key = (p, os.path.getmtime(p))
            if key not in cache:
                try:
                    _, img = tt.decode_mip0(p, 512) if 'max_size' in tt.decode_mip0.__code__.co_varnames else tt.decode_mip0(p)
                    img.thumbnail((512, 512))
                    cache[key] = np.asarray(img.convert('RGBA'))
                except Exception:
                    cache[key] = None
            if cache[key] is not None:
                out[t] = cache[key]
        return out

    # --- levels (the Levels tab)
    def level_index(self):
        """{"Area/Level": [[actor, model, x, y, z, rx, ry, rz, sx, sy, sz], ...]} from the cache, or None."""
        if getattr(self, '_levels', None) is not None:
            return self._levels
        try:
            j = json.load(open(os.path.join(os.path.dirname(self.index_path), 'level_index.json'), encoding='utf-8'))
            if norm(j['romfs']) != norm(self.cfg['romfs']):
                return None
            self._levels = j['levels']
            return self._levels
        except (OSError, ValueError, KeyError):
            return None

    def build_level_index(self):
        script = os.path.join(HERE, 'level_index.py')
        if not os.path.exists(script):
            raise ToolError('level_index.py is missing next to this program.')
        r = subprocess.run(tt.PY_ZSTD + ['-I', script, self.cfg['romfs'],
                                         os.path.join(os.path.dirname(self.index_path), 'level_index.json')],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        if r.returncode:
            raise ToolError('Building the level list failed:\n' + (r.stderr or r.stdout).strip()[-800:])
        self._levels = None
        if self.level_index() is None:
            raise ToolError('The level list was not written.')
        return len(self._levels)

    def unzs_many(self, pairs):
        """Unpack many .zs files in one helper run: pairs of (source, destination)."""
        with tempfile.TemporaryDirectory() as tmp:
            lst = os.path.join(tmp, 'list.txt')
            with open(lst, 'w', encoding='utf-8') as fp:
                fp.write('\n'.join('%s\t%s' % p for p in pairs))
            subprocess.run(tt.PY_ZSTD + ['-I', os.path.join(HERE, 'zs_helper.py'), 'D', tt.game_romfs(None), lst, '-'],
                           capture_output=True, text=True, creationflags=NO_WINDOW)

    def load_level(self, name, say=lambda text: None, far=False):
        """(level model, light copy for moving, {texture: RGBA array}) for a level of the level list. far: include the
        distant scenery (mountains, far-off towns) that surrounds outdoor levels."""
        import bfres_model
        import level_view
        rows = (self.level_index() or {}).get(name)
        if rows is None:
            raise ToolError('%s is not in the level list.' % name)
        cache = getattr(self, '_models', None)
        if cache is None:
            cache = self._models = {}
        skip = None if far else level_view.is_far
        want = sorted({r[1] for r in rows if r[1] and r[1] not in cache and r[1] not in getattr(self, '_flat', ())
                       and (far or not level_view.is_far(r))})
        if want:
            say('Reading %d models of %s...' % (len(want), name))
            with tempfile.TemporaryDirectory() as tmp:
                self.unzs_many([(os.path.join(self.cfg['romfs'], 'Model', m + '.bfres.zs'), os.path.join(tmp, '%d.bin' % i))
                                for i, m in enumerate(want)])
                for i, m in enumerate(want):
                    try:
                        cache[m] = bfres_model.parse(open(os.path.join(tmp, '%d.bin' % i), 'rb').read(), m)
                    except Exception:  # noqa: BLE001 - unreadable models are left out of the level
                        cache[m] = None
        say('Putting %s together...' % name)
        model = level_view.build(name, rows, cache.get, skip)
        say('Reading the textures of %s...' % name)
        tex = self.level_textures(model, say)
        return model, level_view.light(model), tex

    def level_textures(self, model, say=lambda text: None):
        """{texture: small RGBA array} for a level's colour textures (the copy the game would load). The game's own
        copies are kept small on disk after the first time, so a level opens quickly the next time."""
        import numpy as np
        from PIL import Image
        order, off = self.mod_order()
        live = [m for m in reversed(order) if m not in off and m != SIZE_MOD]
        thumbs = os.path.join(os.path.dirname(self.index_path), 'level_textures')
        os.makedirs(thumbs, exist_ok=True)
        names = sorted({next((t for t in m.textures if t.endswith('_Alb')), None) for m in model.meshes} - {None})
        out, todo = {}, []
        for t in names:
            p = next((q for q in (os.path.join(self.cfg['mods'], mod, 'romfs', 'Tex', t + '.bntx.zs') for mod in live)
                      if os.path.exists(q)), self.path(t + '.bntx.zs'))
            if not os.path.exists(p):
                continue
            thumb = os.path.join(thumbs, t + '.png')
            if p == self.path(t + '.bntx.zs') and os.path.exists(thumb) and os.path.getmtime(thumb) >= os.path.getmtime(p):
                try:
                    out[t] = np.asarray(Image.open(thumb).convert('RGBA'))
                    continue
                except OSError:
                    pass
            todo.append((t, p, thumb if p == self.path(t + '.bntx.zs') else None))
        if todo:
            with tempfile.TemporaryDirectory() as tmp:
                self.unzs_many([(p, os.path.join(tmp, '%d.bin' % i)) for i, (_, p, _) in enumerate(todo)])
                for i, (t, p, thumb) in enumerate(todo):
                    if i % 20 == 0:
                        say('Reading the textures of %s: %d of %d (only the first time)...' % (model.name, i, len(todo)))
                    try:
                        _, img = tt.decode_data(open(os.path.join(tmp, '%d.bin' % i), 'rb').read(), 64, tmp)
                        img.thumbnail((64, 64))
                        img = img.convert('RGBA')
                        if thumb:
                            img.save(thumb)
                        out[t] = np.asarray(img)
                    except Exception:  # noqa: BLE001 - shown grey
                        pass
        return out

    def mods(self):
        try:
            return sorted((d for d in os.listdir(self.cfg['mods']) if os.path.isdir(os.path.join(self.cfg['mods'], d))
                           and d != SIZE_MOD), key=str.lower)   # the size table mod is the tool's own, not a place for textures
        except OSError:
            return []

    def size_mod_exists(self):
        return bool(SIZE_MOD) and os.path.isdir(os.path.join(self.cfg['mods'], SIZE_MOD))

    def rebuild_sizes(self):
        """Rebuild the merged resource size table for all mods (after a mod was removed or edited by hand)."""
        if not hasattr(tt, 'merge_sizes'):
            raise ToolError('This version of texture_tool.py has no size table support.')
        mods = self.cfg['mods']
        self.safe_mod_dir(SIZE_MOD)   # the same folder checks as an import
        try:
            return tt.merge_sizes(mods, self.cfg['romfs'])
        except TOOL_ERRORS as e:
            raise ToolError(str(e))

    def mod_state(self, mod):
        """'on', 'off' or 'not listed' from mods.ini (the Mods tab writes it)."""
        order, off = self.mod_order()
        return 'off' if mod in off else 'on'

    def safe_mod_dir(self, mod):
        bad = set('<>:"/\\|?*')
        if not mod or mod.strip() != mod or mod in ('.', '..') or any(c in bad or ord(c) < 32 for c in mod) or mod.endswith('.'):
            raise ToolError('"%s" is not a usable folder name. Avoid  < > : " / \\ | ? *  and leading or trailing spaces.' % mod)
        if '|' in mod or '=' in mod:
            raise ToolError('Mod names cannot contain | or = (mods.ini uses them).')
        mods = self.cfg['mods']
        dst = os.path.join(mods, mod)
        game = os.path.dirname(self.cfg['romfs'])
        merged = os.path.dirname(os.path.abspath(mods))
        for p in (self.cfg['romfs'], os.path.join(merged, 'bin'), os.path.join(merged, 'work'),
                  os.path.join(game, 'merged', 'bin'), os.path.join(game, 'merged', 'work')):
            if inside(dst, p) or inside(p, dst):
                raise ToolError('Refusing to write there: the mods folder overlaps the game files (%s).\nCheck Settings.' % p)
        return dst

    def check_import(self, info, png):
        from PIL import Image
        if not info['importable']:
            raise ToolError('%s is %s.\n\n%s' % (info['name'], info['format'], info['why_not']))
        if png.lower().endswith('.dds'):
            if not hasattr(tt, 'read_dds'):
                raise ToolError('This version of texture_tool.py cannot import .dds files; use a PNG.')
            try:
                dxgi, w, h, _ = tt.read_dds(png)
            except TOOL_ERRORS as e:
                raise ToolError(str(e))
            want, got = (0x20 if info['astc'] else info['kind']), tt.DXGI_KIND[dxgi]
            if got != want:
                raise ToolError('Wrong DDS format: it is %s, but %s needs %s%s.\n\nSave the DDS as %s, or import a PNG.' % (
                    tt.DXGI_NAME[got], info['name'], tt.DXGI_NAME[want],
                    ' (the mod\'s copy of an ASTC texture is BC7)' if info['astc'] else '', tt.DXGI_NAME[want]))
            if (w, h) != (info['w'], info['h']):
                raise ToolError('Wrong DDS size: it is %d x %d, but %s is %d x %d.' % (w, h, info['name'], info['w'], info['h']))
            return
        try:
            with Image.open(png) as im:
                size = im.size
        except Exception as e:
            raise ToolError('Could not read the PNG:\n%s\n\n%s' % (png, e))
        if size != (info['w'], info['h']):
            raise ToolError('Wrong PNG size: it is %d x %d, but %s is %d x %d.\n\nResize the PNG to exactly %d x %d (keep the '
                            'exported PNG as a starting point).' % (size[0], size[1], info['name'], info['w'], info['h'], info['w'], info['h']))

    def do_import(self, info, png, mod):
        self.check_import(info, png)
        if SIZE_MOD and mod.lower() == SIZE_MOD.lower():
            raise ToolError('"%s" is kept by the tool for the size table. Pick another mod name.' % SIZE_MOD)
        dst = self.safe_mod_dir(mod)
        os.makedirs(dst, exist_ok=True)
        try:
            tt.import_png(self.path(info['name']), png, dst)
        except TOOL_ERRORS as e:
            raise ToolError(str(e))
        return os.path.join(dst, 'romfs', 'Tex', info['name'])


class App:
    MOD_MARK = '   \u25c6'   # after a name in the list: some mod replaces this texture

    def __init__(self, root, cfg):
        self.root, self.cfg = root, cfg
        self.be = Backend(cfg)
        self.names, self.shown = [], []
        self.info, self.orig, self.img, self.photo = None, None, None, None
        self.name, self.path, self.versions = None, None, []
        self.modded = {}
        self.jobs = queue.Queue()
        self.busy = False
        self.cache = {}
        self.want = None

        root.title('OOT texture tool')
        f = root.winfo_fpixels('1i') / 96   # display scaling
        root.geometry('%dx%d' % (min(1000 * f, root.winfo_screenwidth() * 0.9), min(760 * f, root.winfo_screenheight() * 0.85)))
        root.minsize(int(760 * f), int(520 * f))
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')

        top = ttk.Frame(root, padding=(8, 8, 8, 0))
        top.pack(fill='x')
        ttk.Label(top, text='Search:').pack(side='left')
        self.q = tk.StringVar()
        self.q.trace_add('write', lambda *_: self.filter())
        e = ttk.Entry(top, textvariable=self.q, width=34)
        e.pack(side='left', padx=(4, 8))
        e.focus_set()
        self.only_mod = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='Modded only', variable=self.only_mod, command=self.filter).pack(side='left')
        self.count = ttk.Label(top, text='')
        self.count.pack(side='left', padx=(8, 0))
        ttk.Button(top, text='Settings...', command=self.settings).pack(side='right')
        ttk.Button(top, text='Open mods folder', command=self.open_mods).pack(side='right', padx=4)
        ttk.Button(top, text='Import folder...', command=self.import_folder).pack(side='right')
        if SIZE_MOD:
            ttk.Button(top, text='Rebuild size table', command=self.rebuild_sizes).pack(side='right')

        body = ttk.PanedWindow(root, orient='horizontal')
        body.pack(fill='both', expand=True, padx=8, pady=8)
        self.tabs = ttk.Notebook(body)
        left = ttk.Frame(self.tabs)
        self.lb = tk.Listbox(left, activestyle='none', exportselection=False, font=('Segoe UI', 9))
        sb = ttk.Scrollbar(left, orient='vertical', command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.lb.bind('<<ListboxSelect>>', lambda _: self.select())
        self.tabs.add(left, text='Textures')
        mtab = ttk.Frame(self.tabs)
        mtop = ttk.Frame(mtab)
        mtop.pack(fill='x', pady=(4, 4))
        ttk.Label(mtop, text='Find:').pack(side='left')
        self.mq = tk.StringVar()
        self.mq.trace_add('write', lambda *_: self.fill_models())
        ttk.Entry(mtop, textvariable=self.mq, width=20).pack(side='left', padx=4, fill='x', expand=True)
        self.mcount = ttk.Label(mtop, text='')
        self.mcount.pack(side='left')
        mlist = ttk.Frame(mtab)
        mlist.pack(fill='both', expand=True)
        self.mlb = tk.Listbox(mlist, activestyle='none', exportselection=False, font=('Segoe UI', 9))
        msb = ttk.Scrollbar(mlist, orient='vertical', command=self.mlb.yview)
        self.mlb.config(yscrollcommand=msb.set)
        self.mlb.pack(side='left', fill='both', expand=True)
        msb.pack(side='right', fill='y')
        self.mlb.bind('<<ListboxSelect>>', lambda _: self.models_tab_select())
        self.tabs.add(mtab, text='Models')
        ltab = ttk.Frame(self.tabs)
        ltop = ttk.Frame(ltab)
        ltop.pack(fill='x', pady=(4, 4))
        ttk.Label(ltop, text='Find:').pack(side='left')
        self.lq = tk.StringVar()
        self.lq.trace_add('write', lambda *_: self.fill_levels())
        ttk.Entry(ltop, textvariable=self.lq, width=20).pack(side='left', padx=4, fill='x', expand=True)
        self.lcount = ttk.Label(ltop, text='')
        self.lcount.pack(side='left')
        self.far = tk.BooleanVar(value=False)
        ttk.Checkbutton(ltab, text='Show far scenery (distant mountains, sky)', variable=self.far,
                        command=lambda: self.level and self.open_level(self.level)).pack(anchor='w', side='bottom')
        llist = ttk.Frame(ltab)
        llist.pack(fill='both', expand=True)
        self.llb = tk.Listbox(llist, activestyle='none', exportselection=False, font=('Segoe UI', 9))
        lsb = ttk.Scrollbar(llist, orient='vertical', command=self.llb.yview)
        self.llb.config(yscrollcommand=lsb.set)
        self.llb.pack(side='left', fill='both', expand=True)
        lsb.pack(side='right', fill='y')
        self.llb.bind('<<ListboxSelect>>', lambda _: self.levels_tab_select())
        self.tabs.add(ltab, text='Levels')
        self.tabs.bind('<<NotebookTabChanged>>', lambda _: self.tab_changed())
        self.models_all, self.levels_all = [], []
        self.level, self.level_light, self.level_pan = None, None, None
        body.add(self.tabs, weight=1)

        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        self.title = ttk.Label(right, text='Pick a texture on the left.', font=('Segoe UI', 11, 'bold'))
        self.title.pack(anchor='w')
        self.details = ttk.Label(right, text='', justify='left')
        self.details.pack(anchor='w')
        self.canimport = ttk.Label(right, text='', justify='left', wraplength=int(PREVIEW * f))
        self.canimport.pack(anchor='w', pady=(2, 6))
        vrow = ttk.Frame(right)
        vrow.pack(anchor='w', fill='x', pady=(0, 6))
        ttk.Label(vrow, text='Show:').pack(side='left')
        self.ver = tk.StringVar()
        self.vercb = ttk.Combobox(vrow, textvariable=self.ver, state='disabled', width=58)
        self.vercb.pack(side='left', padx=(4, 0))
        self.vercb.bind('<<ComboboxSelected>>', lambda _: self.pick_version())
        mrow = ttk.Frame(right)
        mrow.pack(anchor='w', fill='x', pady=(0, 6))
        self.view = tk.StringVar(value='texture')
        ttk.Label(mrow, text='View:').pack(side='left')
        ttk.Radiobutton(mrow, text='Texture', value='texture', variable=self.view, command=self.set_view).pack(side='left', padx=(4, 0))
        ttk.Radiobutton(mrow, text='On model', value='model', variable=self.view, command=self.set_view).pack(side='left', padx=(4, 8))
        self.model = tk.StringVar()
        self.modelcb = ttk.Combobox(mrow, textvariable=self.model, state='disabled', width=46)
        self.modelcb.pack(side='left')
        self.modelcb.bind('<<ComboboxSelected>>', lambda _: self.pick_model())
        self.bmexp = ttk.Button(mrow, text='Export its textures...', command=self.export_model_textures, state='disabled')
        self.bmexp.pack(side='left', padx=(6, 0))
        prow = ttk.Frame(right)
        prow.pack(anchor='w', fill='x', pady=(0, 6))
        self.painting = tk.BooleanVar(value=False)
        ttk.Checkbutton(prow, text='Paint on model', variable=self.painting, command=self.toggle_paint).pack(side='left')
        self.brush_rgb = (220, 60, 140)
        self.colour_swatch = tk.Button(prow, width=3, background='#dc3c8c', relief='groove', command=self.pick_colour)
        self.colour_swatch.pack(side='left', padx=(8, 4))
        ttk.Label(prow, text='Brush').pack(side='left')
        self.brush = tk.IntVar(value=8)
        ttk.Scale(prow, from_=1, to=80, variable=self.brush, length=110,
                  command=lambda v: self.brush.set(int(float(v)))).pack(side='left', padx=4)
        ttk.Button(prow, text='Undo', command=self.undo_paint).pack(side='left', padx=(4, 0))
        ttk.Button(prow, text='Import painted...', command=self.import_painted).pack(side='left', padx=(6, 0))
        self.undo, self.browse, self.pickbuf, self.cursor_xy, self.painted = [], False, None, None, False
        self.mdl, self.mdl_tex, self.yaw, self.pitch, self.zoom, self.drag = None, {}, 0.5, -0.2, 1.0, None
        self.override = None      # a recoloured or edited image shown instead of the texture (not imported yet)
        self.tzoom, self.tpan = 1.0, (0.0, 0.0)   # texture view zoom (wheel) and offset (drag)
        self.recolour_win = None
        self.edit_file, self.edit_mtime = None, None
        btns = ttk.Frame(right)
        btns.pack(anchor='w', pady=(0, 6))
        self.bexp = ttk.Button(btns, text='Export PNG...', command=lambda: self.export('.png'), state='disabled')
        self.bexp.pack(side='left')
        self.bdds = ttk.Button(btns, text='Export DDS...', command=lambda: self.export('.dds'), state='disabled')
        self.bdds.pack(side='left', padx=(6, 0))
        self.bimp = ttk.Button(btns, text='Import PNG or DDS...', command=self.import_, state='disabled')
        self.bimp.pack(side='left', padx=6)
        self.brec = ttk.Button(btns, text='Recolour...', command=self.recolour, state='disabled')
        self.brec.pack(side='left')
        self.bedit = ttk.Button(btns, text='Edit in paint program', command=self.edit_outside, state='disabled')
        self.bedit.pack(side='left', padx=6)
        self.bdel = ttk.Button(btns, text='Remove from mod', command=self.remove_copy, state='disabled')
        self.bdel.pack(side='left')
        self.canvas = tk.Canvas(right, width=int(PREVIEW * f), height=int(PREVIEW * f), highlightthickness=1,
                                highlightbackground='#aaa', bg='#ddd')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _: self.draw())
        self.canvas.bind('<ButtonPress-1>', self.drag_start)
        self.canvas.bind('<B1-Motion>', self.drag_move)
        self.canvas.bind('<ButtonRelease-1>', self.drag_end)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<ButtonPress-3>', self.rotate_start)
        self.canvas.bind('<B3-Motion>', self.rotate_move)
        self.canvas.bind('<ButtonRelease-3>', self.rotate_end)
        self.canvas.bind('<Motion>', self.hover)
        self.canvas.bind('<ButtonPress-2>', self.pan_start)      # levels: middle-drag (or Shift+drag) moves the view
        self.canvas.bind('<B2-Motion>', self.pan_move)
        self.canvas.bind('<ButtonRelease-2>', lambda e: self.schedule_draw())
        self.canvas.bind('<Double-Button-1>', self.centre_on)
        body.add(right, weight=2)

        self.status = ttk.Label(root, text='', relief='sunken', anchor='w', padding=(6, 2))
        self.status.pack(fill='x', side='bottom')

        root.after(50, self.startup)
        root.after(100, self.poll)

    # --- background work (decoding and importing call helper programs; keep the window responsive)
    def work(self, label, fn, done):
        self.busy = True
        self.say(label)
        self.root.config(cursor='watch')

        def go():
            try:
                self.jobs.put((done, fn(), None))
            except Exception as e:  # noqa: BLE001 - shown to the owner
                self.jobs.put((done, None, e))
        threading.Thread(target=go, daemon=True).start()

    def poll(self):
        if self.busy and getattr(self, 'progress', None):
            self.say(self.progress)
            self.progress = None
        try:
            while True:
                done, res, err = self.jobs.get_nowait()
                self.busy = False
                self.root.config(cursor='')
                done(res, err)
        except queue.Empty:
            pass
        self.root.after(60, self.poll)

    def say(self, text):
        self.status.config(text=text)

    def error(self, title, err):
        msg = str(err) if isinstance(err, ToolError) else '%s: %s' % (type(err).__name__, err)
        if 'texconv' in msg and not os.path.exists(tt.TEXCONV):
            msg += '\n\ntexconv.exe is missing; see Settings.'
        self.say(title)
        messagebox.showerror(title, msg, parent=self.root)

    # --- list
    def startup(self):
        def done(bad, err):
            self.names = self.be.textures()
            self.modded = self.be.modded()
            self.filter()
            if err or bad:
                self.error('Something is missing', err or ToolError('\n\n'.join(bad)))
            else:
                self.say('Ready. %d textures in %s; %d replaced by mods in %s' % (
                    len(self.names), self.be.tex_dir, len(self.modded), self.cfg['mods']))
        self.work('Checking the tools...', self.be.check_tools, done)

    def filter(self):
        words = self.q.get().lower().split()
        only = self.only_mod.get()
        # "model:name" keeps the textures of the models whose name contains it (once the model list is built)
        mwords = [w[6:] for w in words if w.startswith('model:') and len(w) > 6]
        words = [w for w in words if not w.startswith('model:')]
        allowed = None
        if mwords:
            idx = self.be.model_index()
            if idx is None:
                self.say('model: searches need the model list; open View: On model once to build it.')
            else:
                allowed = {t + '.bntx.zs' for m, ts in idx.items() if all(w in m.lower() for w in mwords) for t in ts}
        self.shown = [n for n in self.names if all(w in n.lower() for w in words) and (not only or n in self.modded)
                      and (allowed is None or n in allowed)]
        self.lb.delete(0, 'end')
        self.lb.insert('end', *[n[:-8] + (self.MOD_MARK if n in self.modded else '') for n in self.shown])
        self.count.config(text='%d of %d' % (len(self.shown), len(self.names)))
        if self.name in self.shown:
            i = self.shown.index(self.name)
            self.lb.selection_set(i)
            self.lb.see(i)

    def select(self):
        sel = self.lb.curselection()
        if not sel:
            return
        self.browse = False
        name = self.shown[sel[0]]
        self.versions = self.be.versions(name)
        # what the game shows: the winning mod's copy, or the game's own
        path = next((p for _, p, win in self.versions if win), None)
        self.load(name, path)

    def pick_version(self):
        labels = [v[0] for v in self.versions]
        if self.name and self.ver.get() in labels:
            self.load(self.name, self.versions[labels.index(self.ver.get())][1])

    def load(self, name, path):
        """Show a texture: the game's copy (path None) or a mod's copy."""
        key = (name, path, os.path.getmtime(path) if path and os.path.exists(path) else 0)
        self.want = key
        if key in self.cache:
            self.show(name, path, *self.cache[key])
            return
        if self.busy:
            return  # picked up when the current job ends

        def job():
            info, img = self.be.decode(name, path)
            orig = info if path is None else self.be.decode(name)[0] if (name, None, 0) not in self.cache \
                else self.cache[(name, None, 0)][0]
            return orig, info, img

        def done(res, err):
            if err:
                self.show_error(name, err)
            else:
                self.cache[key] = res
                if len(self.cache) > 40:
                    self.cache.pop(next(iter(self.cache)))
                if self.want == key:
                    self.show(name, path, *res)
            if self.want != key and self.want:
                self.load(*self.want[:2])
        self.work('Reading %s%s...' % (name, '' if path is None else ' (mod copy)'), job, done)

    def show_error(self, name, err):
        self.info, self.orig, self.img = None, None, None
        self.canvas.delete('all')
        self.title.config(text=name[:-8])
        self.details.config(text='Could not read this texture.')
        self.canimport.config(text=str(err), foreground='#a00')
        for b in (self.bexp, self.bdds, self.bimp, self.brec, self.bedit, self.bdel):
            b.config(state='disabled')
        self.say('Could not read %s' % name)

    def show(self, name, path, orig, info, img):
        if name != self.name:
            self.end_override()
            self.tzoom, self.tpan = 1.0, (0.0, 0.0)
        self.name, self.path, self.orig, self.info, self.img = name, path, orig, info, img
        scale = self.draw()
        self.title.config(text=name[:-8])
        mod_note = '' if path is None or info['format'] == orig['format'] else \
            '   (the game\'s copy is %s)' % orig['format']
        self.details.config(text='Format: %s%s%s%s\nSize: %d x %d\nMip levels: %d%s' % (
            info['format'], ' (sRGB)' if info['srgb'] else '', ' (signed: shown +128)' if info['snorm'] else '', mod_note,
            info['w'], info['h'], info['mips'], '' if scale == 1 else '   (preview at %d%%)' % round(scale * 100)))
        if orig['importable'] and orig['astc']:
            self.canimport.config(text='Can be imported: yes. The mod\'s copy is saved as BC7 (a bigger file); the tool '
                                       'raises the game\'s size table to match, in the mod "%s".' % SIZE_MOD, foreground='#060')
        elif orig['importable']:
            self.canimport.config(text='Can be imported: yes (stays %s, same file size).' % orig['format'], foreground='#060')
        else:
            self.canimport.config(text='Can be imported: no. ' + orig['why_not'], foreground='#a50')
        labels = [v[0] for v in self.versions] if self.versions and self.versions[0][1] is None else ['Original (game)']
        self.vercb.config(values=labels, state='readonly' if len(labels) > 1 else 'disabled')
        current = next((v[0] for v in self.versions if v[1] == path), labels[0])
        self.ver.set(current)
        self.bexp.config(state='normal')
        self.bdds.config(state='normal' if info['dds'] and hasattr(tt, 'export_dds') else 'disabled')
        self.bimp.config(state='normal')
        self.brec.config(state='normal' if orig['importable'] else 'disabled')
        self.bedit.config(state='normal' if orig['importable'] else 'disabled')
        self.bdel.config(state='normal' if path else 'disabled')
        self.say('%s: %s, %d x %d, %d mip levels; showing %s' % (name, info['format'], info['w'], info['h'], info['mips'],
                                                                 current))
        if self.view.get() == 'model':
            self.root.after(10, self.load_models)

    # --- model view
    def set_view(self):
        if self.view.get() == 'model':
            try:
                import numpy  # noqa: F401
            except ImportError:
                self.view.set('texture')
                self.error('numpy missing', ToolError('The model view needs numpy:\n\n  py -3.12 -m pip install numpy'))
                return
            self.load_models()
        else:
            self.modelcb.config(state="disabled"); self.bmexp.config(state="disabled")
            self.draw()

    def load_models(self):
        """Fill the model list for the shown texture (building the model list once if needed), then show the first."""
        if not self.name or self.view.get() != 'model':
            return
        if self.busy:
            return self.root.after(200, self.load_models)
        if self.level:
            slot = self.be.model_slot(self.name)
            if any(slot in m.textures for m in self.mdl.meshes):   # stay in the level, with the texture lit up
                self.draw()
                self.say(getattr(self, 'level_note', None) or '%s in %s: shown in gold, the rest dimmed.' % (slot, self.level))
                self.level_note = None
                return
            self.leave_level()
        models = self.be.models_for(self.name)
        if models is None:
            def done(n, err):
                if err:
                    self.view.set('texture')
                    self.error('Model list not built', err)
                else:
                    self.say('Model list built: %d models use textures' % n)
                    self.load_models()
            self.work('Building the list of models and their textures (once, about 30 seconds)...', self.be.build_model_index, done)
            return
        if not models:
            self.modelcb.config(values=[], state="disabled"); self.bmexp.config(state="disabled")
            self.model.set('No model uses this texture directly')
            self.mdl = None
            self.draw()
            return
        self.modelcb.config(values=models, state="readonly"); self.bmexp.config(state="normal")
        if self.model.get() not in models:
            self.model.set(models[0])
        self.pick_model()   # (re)load: the model is cached; textures follow the mods as they are now

    def pick_model(self):
        name = self.model.get()
        if self.level:
            return self.draw()
        if self.busy:
            return self.root.after(200, self.pick_model)
        if name in getattr(self.be, '_flat', ()):
            import bfres_model
            self.mdl = bfres_model.Model(name, [])   # terrain piece: says so without loading it
            self.say('%s keeps its meshes in a form the preview cannot read yet.' % name)
            self.draw()
            return

        def job():
            m = self.be.load_model(name)
            return m, self.be.model_textures(m)

        def done(res, err):
            if err:
                self.mdl = None
                self.error('Could not show the model', err)
                return
            if self.mdl is None or self.mdl.name != name:
                self.yaw, self.pitch, self.zoom = 0.5, -0.2, 1.0
            self.mdl, self.mdl_tex = res
            if not self.mdl.meshes:
                self.say('%s: no meshes this preview can read' % name)
                self.draw()
                return
            used = sum(1 for m in self.mdl.meshes if self.name and self.be.model_slot(self.name) in m.textures)
            if self.browse or not self.name:
                self.say('%s: %d meshes. Drag to turn, wheel to zoom, click a part to select its texture.'
                         % (name, len(self.mdl.meshes)))
            elif all(m.name.lower().startswith('dummy') for m in self.mdl.meshes):
                self.say('%s is only a placeholder (a %d-triangle "dummy" mesh); in the game this texture is put on another '
                         'model. Try another model in the list.' % (name, sum(len(m.tris) for m in self.mdl.meshes)))
            else:
                self.say('%s: %d meshes, %d use %s. Drag to turn, wheel to zoom.' % (name, len(self.mdl.meshes), used,
                                                                                     self.name[:-8]))
            self.draw()
        self.work('Loading the model %s...' % name, job, done)

    # --- mouse on the preview: turn / zoom the model, pick a part (click), paint (Paint on); move / zoom the texture
    def drag_start(self, e):
        self.drag = (e.x, e.y, self.yaw, self.pitch, self.tpan)
        self.moved = False
        self.pan_drag = bool(self.level and self.view.get() == 'model' and e.state & 0x1 and not self.painting.get())
        if self.pan_drag:
            self.pan_start(e)
            return
        self.last_paint = None
        if self.view.get() == 'model' and self.painting.get() and self.mdl:
            self.undo.append(None if self.override is None else self.override.copy())
            del self.undo[:-12]
            self.paint_at(e.x, e.y)

    def drag_move(self, e):
        if not self.drag:
            return
        x, y, yaw, pitch, pan = self.drag
        if abs(e.x - x) + abs(e.y - y) > 3:
            self.moved = True
        if getattr(self, 'pan_drag', False):
            self.pan_move(e)
            return
        if self.view.get() == 'texture' and self.img is not None and self.tzoom > 1:
            s = self.draw_scale()
            self.tpan = (pan[0] - (e.x - x) / s, pan[1] - (e.y - y) / s)
            self.schedule_draw()
            return
        if self.view.get() == 'model' and self.mdl:
            if self.painting.get():
                self.paint_at(e.x, e.y)
                return
            self.yaw = yaw + (e.x - x) * 0.01
            self.pitch = max(-1.5, min(1.5, pitch + (e.y - y) * 0.01))
            self.schedule_draw(fast=True)

    def drag_end(self, e):
        moved = getattr(self, 'moved', True)
        self.drag = None
        if getattr(self, 'pan_drag', False):
            self.pan_drag = False
            self.schedule_draw()
            return
        if self.view.get() == 'model' and self.mdl:
            if self.painting.get():
                self.schedule_draw()
            elif not moved:
                self.pick_at(e.x, e.y)
            else:
                self.schedule_draw()

    def pan_start(self, e):
        self.pdrag = (e.x, e.y, None if self.level_pan is None else self.level_pan.copy())

    def pan_move(self, e):
        """Levels: move the view (middle-drag or Shift+drag)."""
        if not self.level or getattr(self, 'pdrag', None) is None or self.pdrag[2] is None or self.view.get() != 'model':
            return
        import bfres_model
        x, y, start = self.pdrag
        cw, ch = max(16, self.canvas.winfo_width() - 4), max(16, self.canvas.winfo_height() - 4)
        per_px = 2.1 * self.mdl.radius / (self.zoom * min(cw, ch))      # world units per screen pixel
        r = bfres_model.view_matrix(self.yaw, self.pitch)
        self.level_pan = start - r[0] * (e.x - x) * per_px + r[1] * (e.y - y) * per_px
        self.schedule_draw(fast=True)

    def centre_on(self, e):
        """Levels: double-click centres the view on the actor under the mouse."""
        if not self.level or self.view.get() != 'model' or self.painting.get():
            return
        hit = self.pick_owner(e.x, e.y)
        if hit is not None:
            import numpy as np
            row = self.mdl.rows[hit]
            self.level_pan = np.array(row[2:5], np.float32)
            self.zoom = max(self.zoom, 4.0)
            self.say('Centred on %s. Wheel zooms, middle-drag or Shift+drag moves.' % row[0])
            self.schedule_draw()

    def schedule_draw(self, fast=False):
        """Draw once per screen update, however many mouse events arrive; a sharp draw follows a pause."""
        self._want_fast = fast if getattr(self, '_draw_job', None) is None else (self._want_fast and fast)
        if getattr(self, '_draw_job', None) is None:
            def run():
                self._draw_job = None
                self.draw(fast=self._want_fast)
            self._draw_job = self.root.after(1, run)
        if fast:
            if getattr(self, '_settle_job', None):
                self.root.after_cancel(self._settle_job)
            self._settle_job = self.root.after(300, lambda: (setattr(self, '_settle_job', None), self.draw()))

    def draw_scale(self):
        cw, ch = max(16, self.canvas.winfo_width() - 4), max(16, self.canvas.winfo_height() - 4)
        img = self.override or self.img
        fit = min(cw / img.width, ch / img.height)
        return (max(1, int(fit)) if fit > 1 else fit) * self.tzoom

    def wheel(self, e):
        if self.view.get() == 'texture' and self.img is not None:
            self.tzoom = max(1.0, min(32.0, self.tzoom * (1.25 if e.delta > 0 else 0.8)))
            if self.tzoom == 1.0:
                self.tpan = (0.0, 0.0)
            self.schedule_draw()
            self.say('Texture zoom %d%%%s' % (round(self.draw_scale() * 100), ' - drag to move' if self.tzoom > 1 else ''))
            return
        if self.view.get() == 'model' and self.mdl:
            if self.painting.get() and e.state & 0x4:   # Ctrl+wheel: brush size
                self.brush.set(max(1, min(80, self.brush.get() + (2 if e.delta > 0 else -2))))
                return
            self.zoom = max(0.3, min(300.0 if self.level else 30.0, self.zoom * (1.15 if e.delta > 0 else 1 / 1.15)))
            self.schedule_draw(fast=True)

    def focus_texture(self):
        """The texture the model view shows highlighted (None when browsing a whole model)."""
        if self.browse or not self.name:
            return None
        return self.be.model_slot(self.name)

    def draw_model(self, fast=False):
        import numpy as np
        from PIL import Image, ImageTk
        cw, ch = max(16, self.canvas.winfo_width() - 4), max(16, self.canvas.winfo_height() - 4)
        self.canvas.delete('all')
        self.pickbuf = None
        if self.mdl is None or not self.mdl.meshes:
            msg = (self.model.get() or 'No model') if self.mdl is None else (
                '%s keeps its meshes in a form this preview cannot read yet\n(terrain and some map pieces).\n'
                'Pick another model from the list, or use the Texture view.' % self.mdl.name)
            self.canvas.create_text(cw // 2, ch // 2, text=msg, fill='#555', justify='center')
            return 1
        import bfres_model
        tex = dict(self.mdl_tex)
        hl = self.focus_texture()
        if hl and self.img is not None:
            src = self.override or self.img
            if fast and getattr(self, '_small_src', None) is src:
                small = self._small
            else:
                small = src.copy()
                small.thumbnail((1024, 1024))
                small = np.asarray(small.convert('RGBA'))
                self._small_src, self._small = src, small
            tex[hl] = small
        # draws at up to 900 pixels (450 while moving) and is scaled to the window
        k = max(1.0, max(cw, ch) / (450.0 if fast else 900.0))
        rw, rh = max(16, int(cw / k)), max(16, int(ch / k))
        mdl, frame = self.mdl, None
        if self.level:   # levels: a fixed camera that the owner moves; a coarse copy while moving
            mdl = self.level_light if fast and self.level_light is not None else self.mdl
            frame = (self.level_pan, self.mdl.radius)
        out, bufs = bfres_model.render(mdl, tex, (rw, rh), self.yaw, self.pitch, highlight=hl, zoom=self.zoom,
                                       pick=True, dim_others=not self.painting.get(), frame=frame,
                                       tint=(255, 200, 40) if self.level and not self.painting.get() else None)
        self.pickbuf = (bufs, rw, rh, cw, ch)
        self.drawn = mdl
        img = Image.fromarray(out)
        if (rw, rh) != (cw, ch):
            img = img.resize((cw, ch), Image.NEAREST if fast else Image.BILINEAR)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.photo)
        if self.painting.get() and getattr(self, 'cursor_xy', None):
            x, y = self.cursor_xy
            r = self.brush.get()
            self.canvas.create_oval(x - r, y - r, x + r, y + r, outline='#fff')
        return 1

    def pick(self, x, y):
        """(mesh, u, v, texels per screen pixel) under a point of the preview, or None."""
        if not self.pickbuf:
            return None
        bufs, rw, rh, cw, ch = self.pickbuf
        mid, ub, vb = bufs[:3]
        ox = (self.canvas.winfo_width() - cw) / 2
        oy = (self.canvas.winfo_height() - ch) / 2
        ix, iy = int((x - ox) * rw / cw), int((y - oy) * rh / ch)
        if not (0 <= ix < rw and 0 <= iy < rh) or mid[iy, ix] < 0:
            return None
        i = int(mid[iy, ix])
        # how far the texture moves across one screen pixel (for the brush size): the median over nearby pixels of
        # the same mesh, leaving out jumps across UV seams
        steps = []
        for jx, jy in ((ix + 1, iy), (ix - 1, iy), (ix, iy + 1), (ix, iy - 1), (ix + 2, iy), (ix, iy + 2)):
            if 0 <= jx < rw and 0 <= jy < rh and mid[jy, jx] == i:
                d = max(abs(float(ub[jy, jx] - ub[iy, ix])), abs(float(vb[jy, jx] - vb[iy, ix])))
                d /= max(abs(jx - ix), abs(jy - iy))
                if 0 < d < 0.02:
                    steps.append(d)
        step = sorted(steps)[len(steps) // 2] if steps else 0.002
        return i, float(ub[iy, ix]), float(vb[iy, ix]), step * rw / cw

    def mesh_at(self, i):
        """A mesh by the number the last drawing gave it (a level's coarse copy numbers its meshes differently)."""
        return (getattr(self, 'drawn', None) or self.mdl).meshes[i]

    def pick_owner(self, x, y):
        """Levels: the index row of the actor under a point of the preview, or None."""
        if not self.pickbuf or len(self.pickbuf[0]) < 4:
            return None
        bufs, rw, rh, cw, ch = self.pickbuf
        ox = (self.canvas.winfo_width() - cw) / 2
        oy = (self.canvas.winfo_height() - ch) / 2
        ix, iy = int((x - ox) * rw / cw), int((y - oy) * rh / ch)
        if not (0 <= ix < rw and 0 <= iy < rh) or bufs[3][iy, ix] < 0:
            return None
        return int(bufs[3][iy, ix])

    def pick_at(self, x, y):
        """Click on the model: select the texture of the part under the mouse (the reverse lookup)."""
        hit = self.pick(x, y)
        if hit is None:
            return
        mesh = self.mesh_at(hit[0])
        hl = self.focus_texture()
        tex = hl if hl in mesh.textures else next((t for t in mesh.textures if t.endswith('_Alb')), None) or \
            (mesh.textures[0] if mesh.textures else None)
        listing = ', '.join(mesh.textures) or 'no textures'
        owner = self.pick_owner(x, y) if self.level else None
        if owner is not None:
            row = self.mdl.rows[owner]
            self.level_note = '%s (model %s) at %.0f, %.0f, %.0f uses %s' % (row[0], row[1], row[2], row[3], row[4], listing)
            self.say(self.level_note)
            if tex and tex != hl:
                self.select_texture(tex)
            return
        if not tex:
            self.say('%s (material %s): %s' % (mesh.name, mesh.material, listing))
            return
        self.say('%s (material %s) uses %s' % (mesh.name, mesh.material, listing))
        if tex != hl:
            self.select_texture(tex)

    def select_texture(self, tex):
        """Select a texture in the list (clearing the search if it hides it), keeping the model view."""
        name = tex + '.bntx.zs'
        if name not in self.names:
            self.say('%s is not in the game\'s Tex folder' % tex)
            return
        self.browse = False
        if not self.level:   # in a level, the level list stays in front
            self.tabs.select(0)
        if name not in self.shown:
            self.only_mod.set(False)
            self.q.set('')
        i = self.shown.index(name)
        self.lb.selection_clear(0, 'end')
        self.lb.selection_set(i)
        self.lb.see(i)
        self.select()

    def paint_at(self, x, y):
        """Paint the brush colour into the selected texture where the mouse is on the model."""
        from PIL import ImageDraw
        self.cursor_xy = (x, y)
        hl = self.focus_texture()
        hit = self.pick(x, y)
        if hit is None or not hl or self.img is None:
            self.schedule_draw(fast=True)
            return
        i, u, v, step = hit
        if hl not in self.mesh_at(i).textures:
            self.say('Paint works on %s; that part uses %s.' % (hl, ', '.join(self.mesh_at(i).textures) or 'no texture'))
            return
        if self.override is None:
            self.override = self.img.convert('RGBA').copy()
        W, H = self.override.size
        tx, ty = (u % 1.0) * W, (v % 1.0) * H
        r = max(1.0, min(max(W, H) / 8.0, self.brush.get() * step * max(W, H)))
        col = self.brush_rgb + (255,)
        d = ImageDraw.Draw(self.override)
        last = self.last_paint
        if last and abs(last[0] - tx) < 8 * r + 4 and abs(last[1] - ty) < 8 * r + 4:   # same stroke: join the dabs
            d.line((last[0], last[1], tx, ty), fill=col, width=int(2 * r))
        d.ellipse((tx - r, ty - r, tx + r, ty + r), fill=col)
        # paint changes colour only: the texture's transparency (cut-outs) stays as it was
        if getattr(self, '_alpha_src', None) is not self.img:
            self._alpha_src, self._alpha = self.img, self.img.convert('RGBA').getchannel('A')
        self.override.putalpha(self._alpha)
        self.last_paint = (tx, ty)
        self.painted = True
        self.schedule_draw(fast=True)

    def toggle_paint(self):
        if self.painting.get():
            if self.view.get() != 'model' or not self.mdl or not self.focus_texture():
                self.painting.set(False)
                self.error('Paint on model', ToolError('Pick a texture (Textures list, or click a part of a model), '
                                                       'then View: On model, then Paint.'))
                return
            if not self.orig or not self.orig['importable']:
                self.say('Painting works, but this texture cannot be imported (%s); Save PNG keeps the result.'
                         % (self.orig or {}).get('format', '?'))
            self.say('Paint: left-drag paints %s; right-drag turns the model; wheel zooms; Ctrl+wheel brush size.'
                     % self.focus_texture())
        self.cursor_xy = None
        self.draw()

    def pick_colour(self):
        from tkinter import colorchooser
        c = colorchooser.askcolor(color='#%02x%02x%02x' % self.brush_rgb, parent=self.root, title='Brush colour')
        if c and c[0]:
            self.brush_rgb = tuple(int(v) for v in c[0])
            self.colour_swatch.config(background=c[1])

    def sample_colour(self, e):
        """Right-click with Paint on: pick up the colour under the mouse (from the texture)."""
        hit = self.pick(e.x, e.y)
        if hit is None or not self.focus_texture() or self.focus_texture() not in self.mesh_at(hit[0]).textures:
            return
        src = self.override or self.img
        W, H = src.size
        r, g, b = src.convert('RGB').getpixel((int((hit[1] % 1.0) * W) % W, int((hit[2] % 1.0) * H) % H))
        self.brush_rgb = (r, g, b)
        self.colour_swatch.config(background='#%02x%02x%02x' % (r, g, b))

    def undo_paint(self):
        if not self.undo:
            return
        self.override = self.undo.pop()
        self.draw()

    def import_painted(self):
        if self.override is None:
            self.say('Nothing painted yet.')
            return
        self.import_(self.temp_png(self.override, 'paint'))

    def rotate_start(self, e):
        self.rdrag = (e.x, e.y, self.yaw, self.pitch)

    def rotate_move(self, e):
        if getattr(self, 'rdrag', None) and self.view.get() == 'model' and self.mdl:
            x, y, yaw, pitch = self.rdrag
            self.yaw = yaw + (e.x - x) * 0.01
            self.pitch = max(-1.5, min(1.5, pitch + (e.y - y) * 0.01))
            self.schedule_draw(fast=True)

    def rotate_end(self, e):
        if getattr(self, 'rdrag', None):
            x, y, _, _ = self.rdrag
            self.rdrag = None
            if abs(e.x - x) + abs(e.y - y) <= 3 and self.painting.get():
                self.sample_colour(e)
            else:
                self.schedule_draw()

    def hover(self, e):
        if self.view.get() == 'model' and self.painting.get():
            self.cursor_xy = (e.x, e.y)
            r = self.brush.get()
            self.canvas.delete('brush')
            self.canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline='#fff', tags='brush')

    # --- the Models tab
    def fill_models(self):
        idx = self.be.model_index()
        self.mlb.delete(0, 'end')
        if idx is None:
            self.models_all = []
            self.mlb.insert('end', '(the model list is not built yet:', ' click here, or open View: On model)')
            return
        flat = getattr(self.be, '_flat', set())
        words = self.mq.get().lower().split()
        self.models_all = [m for m in sorted(idx, key=str.lower) if all(w in m.lower() for w in words)]
        self.mlb.insert('end', *[m + ('   (terrain: no preview)' if m in flat else '') for m in self.models_all])
        self.mcount.config(text='%d models' % len(self.models_all))

    def models_tab_select(self):
        sel = self.mlb.curselection()
        if not sel:
            return
        if not self.models_all:
            if self.busy:
                return

            def done(n, err):
                if err:
                    self.error('Model list not built', err)
                else:
                    self.fill_models()
            self.work('Building the list of models and their textures (once, about 30 seconds)...', self.be.build_model_index, done)
            return
        self.open_model(self.models_all[sel[0]])

    def open_model(self, name):
        """Show a whole model (no texture highlighted); click a part to select its texture."""
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.error('numpy missing', ToolError('The model view needs numpy:\n\n  py -3.12 -m pip install numpy'))
            return
        self.leave_level()
        self.browse = True
        self.painting.set(False)
        self.view.set('model')
        self.modelcb.config(values=[name], state='readonly')
        self.bmexp.config(state='normal')
        self.model.set(name)
        self.title.config(text=name)
        idx = self.be.model_index() or {}
        self.details.config(text='%d textures. Click a part of the model to select its texture.' % len(idx.get(name, [])))
        self.canimport.config(text='')
        self.pick_model()

    # --- the Levels tab
    def tab_changed(self):
        i = self.tabs.index('current')
        if i == 1:
            self.fill_models()
        elif i == 2:
            self.fill_levels()

    def fill_levels(self):
        idx = self.be.level_index()
        self.llb.delete(0, 'end')
        if idx is None:
            self.levels_all = []
            self.llb.insert('end', '(the level list is not built yet:', ' click here to build it, about a minute)')
            self.lcount.config(text='')
            return
        words = self.lq.get().lower().split()
        self.levels_all = [n for n in sorted(idx, key=str.lower) if all(w in n.lower() for w in words)]
        self.llb.insert('end', *['%s   (%d placed)' % (n, len(idx[n])) for n in self.levels_all])
        self.lcount.config(text='%d levels' % len(self.levels_all))

    def levels_tab_select(self):
        sel = self.llb.curselection()
        if not sel or self.busy:
            return
        if not self.levels_all:
            def done(n, err):
                if err:
                    self.error('Level list not built', err)
                else:
                    self.say('Level list built: %d levels' % n)
                    self.fill_levels()
            self.work('Building the list of levels and the models placed in them (once, about a minute)...',
                      self.be.build_level_index, done)
            return
        self.open_level(self.levels_all[sel[0]])

    def open_level(self, name):
        """Show a whole level: every placed actor's model where the game puts it. Click a part to select its texture."""
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.error('numpy missing', ToolError('The level view needs numpy:\n\n  py -3.12 -m pip install numpy'))
            return
        if self.be.model_index() is None:   # to know which models are terrain the preview cannot read
            def built(n, err):
                if err:
                    self.error('Model list not built', err)
                else:
                    self.open_level(name)
            self.work('Building the list of models and their textures (once, about 30 seconds)...',
                      self.be.build_model_index, built)
            return

        def job():
            return self.be.load_level(name, lambda text: setattr(self, 'progress', text), self.far.get())

        def done(res, err):
            if err:
                self.error('Could not show the level', err)
                return
            self.leave_level()
            self.mdl, self.level_light, self.mdl_tex = res
            self.level, self.level_pan = name, self.mdl.centre.copy()
            self.browse = True
            self.painting.set(False)
            self.yaw, self.pitch, self.zoom = 0.6, -0.7, 1.5
            self.view.set('model')
            self.modelcb.config(values=['Level: ' + name], state='readonly')
            self.model.set('Level: ' + name)
            self.bmexp.config(state='normal')
            self.title.config(text=name)
            rows = self.mdl.rows
            shown = len({int(o) for m in self.mdl.meshes for o in numpy.unique(m.owner)})
            ntri = sum(len(m.tris) for m in self.mdl.meshes)
            self.details.config(text='%d placed actors, %d drawn (%d triangles); %d textures.\n'
                                     'Terrain stored as "taskmesh" is not drawn yet.' % (
                                         len(rows), shown, ntri, len(self.mdl_tex)))
            self.canimport.config(text='')
            self.draw()
            self.say('%s. Drag to turn, wheel to zoom, middle-drag or Shift+drag to move, double-click to centre on a '
                     'part, click a part to select its texture.' % name)
        self.work('Opening the level %s...' % name, job, done)

    def leave_level(self):
        if self.level:
            self.level, self.level_light, self.level_pan = None, None, None
            self.mdl, self.mdl_tex, self.drawn = None, {}, None

    def draw(self, fast=False):
        """Draw the image to fit the preview area (on a checkerboard, so see-through parts show); returns the scale."""
        from PIL import Image, ImageTk
        if self.view.get() == 'model':
            return self.draw_model(fast)
        img = self.override or self.img
        if img is None or self.info is None:
            return 1
        cw, ch = max(16, self.canvas.winfo_width() - 4), max(16, self.canvas.winfo_height() - 4)
        fit = min(cw / img.width, ch / img.height)
        if fit > 1:
            fit = max(1, int(fit))   # whole steps when enlarging small textures
        scale = fit * self.tzoom
        # the part of the texture in view (wheel zooms, drag moves), scaled to the screen
        cx, cy = img.width / 2 + self.tpan[0], img.height / 2 + self.tpan[1]
        hw, hh = cw / (2 * scale), ch / (2 * scale)
        x0, y0 = max(0, int(cx - hw)), max(0, int(cy - hh))
        x1, y1 = min(img.width, int(math.ceil(cx + hw))), min(img.height, int(math.ceil(cy + hh)))
        self.canvas.delete('all')
        if x1 <= x0 or y1 <= y0:
            return scale
        crop = img.crop((x0, y0, x1, y1))
        size = (max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale)))
        view = crop.resize(size, Image.NEAREST if scale >= 1 else Image.LANCZOS)
        board = Image.new('RGBA', size, (200, 200, 200, 255))
        dark = Image.new('RGBA', (16, 16), (150, 150, 150, 255))
        for y in range(0, size[1], 16):
            for x in range((y // 16) % 2 * 16, size[0], 32):
                board.paste(dark, (x, y))
        board.alpha_composite(view)
        self.photo = ImageTk.PhotoImage(board)
        ox = self.canvas.winfo_width() / 2 + (x0 - cx) * scale
        oy = self.canvas.winfo_height() / 2 + (y0 - cy) * scale
        self.canvas.create_image(ox, oy, image=self.photo, anchor='nw')
        return scale

    # --- buttons
    def export(self, ext):
        info, name, path = self.info, self.name, self.path
        if not info or self.busy:
            return
        kind = 'DDS' if ext == '.dds' else 'PNG'
        mod = '' if path is None else '_' + os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        out = filedialog.asksaveasfilename(parent=self.root, title='Export %s%s' % (kind, '' if path is None else ' (mod copy)'),
                                           initialdir=self.cfg['last_dir'], initialfile=name[:-8] + mod.replace(' ', '_') + ext,
                                           defaultextension=ext, filetypes=[('%s image' % kind, '*' + ext)])
        if not out:
            return
        self.cfg['last_dir'] = os.path.dirname(out)
        save_config(self.cfg)

        def done(_, err):
            if err:
                self.error('Export failed', err)
            else:
                self.say('Exported %s -> %s' % (name, out))
                what = ('%d x %d PNG' % (info['w'], info['h']) if kind == 'PNG' else
                        '%s DDS with %d mip levels' % (info['format'], info['mips']))
                messagebox.showinfo('Exported', 'Saved %s:\n%s\n\nEdit it, keep the same size%s, then use Import.' % (
                    what, out, '' if kind == 'PNG' else ' and format'), parent=self.root)
        self.work('Exporting %s...' % name, lambda: self.be.export(name, out, path), done)

    def import_(self, png=None):
        """Import a file (picked here, or given: a recoloured or edited copy) into a mod of the owner's choice."""
        info, name = self.orig, self.name
        if not info or self.busy:
            return
        if not info['importable']:
            self.error('Cannot import into this texture', ToolError('%s is %s.\n\n%s' % (name, info['format'], info['why_not'])))
            return
        if png is None:
            types = [('PNG or DDS', '*.png *.dds'), ('PNG image', '*.png'), ('DDS texture', '*.dds')] if hasattr(tt, 'read_dds') \
                else [('PNG image', '*.png')]
            png = filedialog.askopenfilename(parent=self.root, title='Pick the edited file for %s (%d x %d)' % (name[:-8], info['w'], info['h']),
                                             initialdir=self.cfg['last_dir'], filetypes=types)
            if not png:
                return
            self.cfg['last_dir'] = os.path.dirname(png)
            save_config(self.cfg)
        try:
            self.be.check_import(info, png)
        except ToolError as e:
            self.error('Cannot import this file', e)
            return
        mod = ModPicker(self.root, self.be.mods()).result
        if not mod:
            return
        try:
            dst = os.path.join(self.be.safe_mod_dir(mod), 'romfs', 'Tex', name)
        except ToolError as e:
            self.error('Cannot use that mod folder', e)
            return
        if os.path.exists(dst) and not messagebox.askyesno(
                'Replace?', 'The mod "%s" already has %s.\nReplace it?' % (mod, name), parent=self.root):
            return

        def done(path, err):
            if err:
                self.error('Import failed', err)
                return
            state = self.be.mod_state(mod)
            how = ('The mod "%s" is on (new mods start on); restart the game to see it.' % mod if state == 'on' else
                   'The mod "%s" is turned OFF: turn it on in the game\'s Mods tab, then restart the game.' % mod)
            self.say('Imported %s into mod "%s"' % (name, mod))
            # show the new copy in place of the old one
            self.modded = self.be.modded()
            self.filter()
            self.versions = self.be.versions(name)
            win = next((label for label, p, w in self.versions if w and p and not inside(p, os.path.join(self.cfg['mods'], mod))), None)
            if win and state == 'on':
                how += ('\n\nBut another mod later in the load order also changes this texture, so the game shows its copy '
                        '(%s). Move "%s" below it in the Mods tab, or turn that mod off.' % (win, mod))
            self.end_override()
            self.load(name, path)
            messagebox.showinfo('Imported', 'Done. Wrote:\n%s\n\n%s%s\n\nThe preview now shows the mod\'s copy; use Show to '
                                'compare it with the original.' % (path, how, self.size_note()), parent=self.root)
        self.work('Importing into %s (encoding every mip level, this can take a little while)...' % mod,
                  lambda: self.be.do_import(info, png, mod), done)

    def import_folder(self):
        """Import every PNG/DDS in a folder whose name is a texture's (as Export its textures... writes them) into one
        mod. Files that match no texture, cannot be imported or have the wrong size are listed, not imported."""
        if self.busy or not self.names:
            return
        folder = filedialog.askdirectory(parent=self.root, title='Folder with edited PNG or DDS files named like the textures',
                                         initialdir=self.cfg['last_dir'])
        if not folder:
            return
        files = sorted(f for f in os.listdir(folder) if f.lower().endswith(('.png', '.dds')))
        known = set(self.names)
        pairs = [(f, f[:-4] + '.bntx.zs') for f in files if f[:-4] + '.bntx.zs' in known]
        skipped = [f for f in files if f[:-4] + '.bntx.zs' not in known]
        if not pairs:
            self.error('Nothing to import', ToolError('No PNG or DDS file in\n%s\nis named like a texture '
                                                     '(for example Mt_Hairs_Player_Adult_Alb.png).' % folder))
            return
        if not messagebox.askyesno('Import folder?', 'Import %d files from\n%s\ninto one mod?%s' % (
                len(pairs), folder, ('\n\n%d other files are not named like a texture and will be skipped.' % len(skipped))
                if skipped else ''), parent=self.root):
            return
        mod = ModPicker(self.root, self.be.mods()).result
        if not mod:
            return
        try:
            self.be.safe_mod_dir(mod)
        except ToolError as e:
            self.error('Cannot use that mod folder', e)
            return
        self.cfg['last_dir'] = folder
        save_config(self.cfg)

        def job():
            done_n, problems = 0, []
            for i, (f, tex) in enumerate(pairs):
                self.progress = 'Importing %d of %d: %s...' % (i + 1, len(pairs), f)
                try:
                    info, _ = self.be.decode(tex)
                    self.be.do_import(info, os.path.join(folder, f), mod)
                    done_n += 1
                except Exception as e:  # noqa: BLE001 - listed to the owner
                    problems.append('%s: %s' % (f, str(e).splitlines()[0]))
            return done_n, problems

        def done(res, err):
            if err:
                self.error('Import failed', err)
                return
            n, problems = res
            self.modded = self.be.modded()
            self.filter()
            self.say('Imported %d files into mod "%s"' % (n, mod))
            msg = 'Imported %d of %d files into the mod "%s".' % (n, len(pairs), mod)
            if problems:
                msg += '\n\nNot imported:\n' + '\n'.join(problems[:12]) + ('\n...' if len(problems) > 12 else '')
            if skipped:
                msg += '\n\nSkipped (not named like a texture): ' + ', '.join(skipped[:8]) + ('...' if len(skipped) > 8 else '')
            messagebox.showinfo('Import folder', msg + self.size_note(), parent=self.root)
            if self.name:
                self.versions = self.be.versions(self.name)
                self.load(self.name, next((p for _, p, win in self.versions if win), None))
        self.work('Importing %d files into %s...' % (len(pairs), mod), job, done)

    def remove_copy(self):
        """Delete the shown mod's copy of this texture (the game's own files are never touched)."""
        name, path = self.name, self.path
        if not path or self.busy:
            return
        mod = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        if not inside(path, self.cfg['mods']) or inside(path, self.cfg['romfs']):
            self.error('Not removed', ToolError('That file is not in the mods folder.'))
            return
        if not messagebox.askyesno('Remove from mod?', 'Delete the mod "%s"\'s copy of %s?\n\n%s\n\nThe game\'s own texture is '
                                   'not touched; this mod just stops changing it. This cannot be undone (export it first '
                                   'to keep it).' % (mod, name[:-8], path), parent=self.root):
            return

        def job():
            os.remove(path)
            for d in (os.path.dirname(path), os.path.dirname(os.path.dirname(path))):   # Tex, romfs when empty
                try:
                    os.rmdir(d)
                except OSError:
                    break
            if self.be.size_mod_exists():
                self.be.rebuild_sizes()

        def done(_, err):
            if err:
                self.error('Not removed', err)
                return
            self.say('Removed %s from the mod "%s"' % (name[:-8], mod))
            self.modded = self.be.modded()
            self.filter()
            self.versions = self.be.versions(name)
            self.load(name, next((p for _, p, win in self.versions if win), None))
        self.work('Removing %s from %s...' % (name[:-8], mod), job, done)

    def export_model_textures(self):
        """Every texture the shown model uses, as PNGs in a folder (for painting a whole character)."""
        model = self.model.get()
        idx = self.be.model_index() or {}
        names = idx.get(model)
        if self.level:   # every texture the level's models use
            model = self.level
            names = sorted({t for m in self.mdl.meshes for t in m.textures if os.path.exists(self.be.path(t + '.bntx.zs'))})
        if not names or self.busy:
            return
        folder = filedialog.askdirectory(parent=self.root, title='Folder for the %d textures of %s' % (len(names), model),
                                         initialdir=self.cfg['last_dir'])
        if not folder:
            return
        self.cfg['last_dir'] = folder
        save_config(self.cfg)

        def job():
            done_n, failed = 0, []
            for t in names:
                try:
                    tt.export_png(self.be.path(t + '.bntx.zs'), os.path.join(folder, t + '.png'))
                    done_n += 1
                except Exception as e:  # noqa: BLE001 - listed to the owner
                    failed.append('%s: %s' % (t, e))
            return done_n, failed

        def done(res, err):
            if err:
                self.error('Export failed', err)
                return
            n, failed = res
            self.say('Exported %d textures of %s to %s' % (n, model, folder))
            messagebox.showinfo('Exported', 'Saved %d of %d textures of %s in\n%s%s' % (
                n, len(names), model, folder, ('\n\nNot saved:\n' + '\n'.join(failed[:10])) if failed else ''), parent=self.root)
        self.work('Exporting the %d textures of %s...' % (len(names), model), job, done)

    # --- recolour and outside editing (previewed on the texture and the model before importing)
    def end_override(self):
        self.override = None
        self.edit_file = None
        if self.recolour_win is not None:
            try:
                self.recolour_win.destroy()
            except tk.TclError:
                pass
            self.recolour_win = None

    def temp_png(self, img, tag):
        d = os.path.join(os.environ.get('LOCALAPPDATA', HERE), 'oot-texture-gui', 'edit')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, '%s_%s.png' % (self.name[:-8], tag))
        img.save(path)
        return path

    def recolour(self):
        if not self.info or self.img is None or self.busy:
            return
        if self.recolour_win is not None:
            self.recolour_win.lift()
            return
        base = self.img.convert('RGBA')
        small = base.copy()
        small.thumbnail((1024, 1024))
        w = self.recolour_win = tk.Toplevel(self.root)
        w.title('Recolour %s' % self.name[:-8])
        w.transient(self.root)
        w.resizable(False, False)
        f = ttk.Frame(w, padding=10)
        f.pack(fill='both')
        hue, sat, val = tk.DoubleVar(value=0), tk.DoubleVar(value=100), tk.DoubleVar(value=100)
        pending = [None]

        def apply(img):
            from PIL import Image
            h, s, v = img.convert('RGB').convert('HSV').split()
            dh = int(round(hue.get() / 360.0 * 256)) % 256
            h = h.point(lambda x: (x + dh) % 256)
            s = s.point(lambda x: max(0, min(255, int(x * sat.get() / 100.0))))
            v = v.point(lambda x: max(0, min(255, int(x * val.get() / 100.0))))
            out = Image.merge('HSV', (h, s, v)).convert('RGB').convert('RGBA')
            out.putalpha(img.getchannel('A'))
            return out

        def update(*_):
            if pending[0]:
                self.root.after_cancel(pending[0])

            def go():
                pending[0] = None
                self.override = apply(small).resize(base.size) if small.size != base.size else apply(small)
                self.draw()
            pending[0] = self.root.after(120, go)

        rows = [('Hue (degrees)', hue, -180, 180), ('Saturation (%)', sat, 0, 300), ('Brightness (%)', val, 0, 300)]
        for r, (label, var, lo, hi) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky='w', padx=(0, 8))
            ttk.Scale(f, from_=lo, to=hi, variable=var, length=280, command=update).grid(row=r, column=1, sticky='we')
            ttk.Label(f, textvariable=var, width=6).grid(row=r, column=2, sticky='e')
            var.trace_add('write', lambda *_ , v=var: v.set(round(v.get())) if v.get() != round(v.get()) else None)
        ttk.Label(f, text='The preview (texture or model) shows the result. Nothing is written until you import.',
                  foreground='#555').grid(row=3, column=0, columnspan=3, sticky='w', pady=(6, 6))
        b = ttk.Frame(f)
        b.grid(row=4, column=0, columnspan=3, sticky='e')

        def reset():
            hue.set(0); sat.set(100); val.set(100); update()

        def do_import():
            full = apply(base)
            path = self.temp_png(full, 'recolour')
            self.recolour_win.destroy()
            self.recolour_win = None
            self.import_(path)

        def save():
            out = filedialog.asksaveasfilename(parent=w, title='Save recoloured PNG', initialdir=self.cfg['last_dir'],
                                               initialfile=self.name[:-8] + '_recolour.png', defaultextension='.png',
                                               filetypes=[('PNG image', '*.png')])
            if out:
                apply(base).save(out)
                self.say('Saved %s' % out)

        def cancel():
            self.end_override()
            self.draw()

        ttk.Button(b, text='Reset', command=reset).pack(side='left')
        ttk.Button(b, text='Save PNG...', command=save).pack(side='left', padx=6)
        ttk.Button(b, text='Import into mod...', command=do_import).pack(side='left')
        ttk.Button(b, text='Cancel', command=cancel).pack(side='left', padx=(6, 0))
        w.protocol('WM_DELETE_WINDOW', cancel)

    def edit_outside(self):
        """Open the shown copy in the default paint program; each time it is saved, the preview (and model) update, and
        Import takes it."""
        if not self.info or self.img is None or self.busy:
            return
        self.end_override()
        path = self.temp_png(self.img.convert('RGBA'), 'edit')
        self.edit_file, self.edit_mtime = path, os.path.getmtime(path)
        try:
            os.startfile(path, 'edit')
        except OSError:
            try:
                os.startfile(path)
            except OSError as e:
                self.error('Could not open a paint program', ToolError('%s\n\nOpen this file yourself:\n%s' % (e, path)))
        self.say('Editing %s in your paint program. Save it there; the preview updates. Then click Import edited file.' % path)
        self.bimp.config(text='Import edited file', command=lambda: self.import_(self.edit_file))
        self.root.after(1000, self.watch_edit)

    def watch_edit(self):
        if not self.edit_file:
            self.bimp.config(text='Import PNG or DDS...', command=self.import_)
            return
        try:
            m = os.path.getmtime(self.edit_file)
            if m != self.edit_mtime:
                from PIL import Image
                self.edit_mtime = m
                img = Image.open(self.edit_file)
                img.load()
                if img.size != self.img.size:
                    self.say('The edited file is %d x %d; it must stay %d x %d.' % (img.size + self.img.size))
                else:
                    self.override = img.convert('RGBA')
                    self.draw()
                    self.say('Edited copy saved at %s: shown in the preview. Click Import edited file to put it in a mod.'
                             % time_now())
        except (OSError, ValueError):
            pass
        self.root.after(1000, self.watch_edit)

    def size_note(self):
        """What to do about the size table mod, if there is one."""
        if not self.be.size_mod_exists():
            return ''
        state = self.be.mod_state(SIZE_MOD)
        return ('\n\nThe size table mod "%s" was updated. Keep it %s last in the Mods tab.'
                % (SIZE_MOD, 'on and' if state == 'on' else 'turned ON and'))

    def rebuild_sizes(self):
        if self.busy:
            return

        def done(raised, err):
            if err:
                self.error('Size table not rebuilt', err)
                return
            self.say('Size table rebuilt: %d entries raised' % raised)
            msg = ('Rebuilt: %d entries raised for the mods in\n%s' % (raised, self.cfg['mods']) if raised else
                   'No mod needs a bigger size table entry, so no size table mod is needed.')
            messagebox.showinfo('Size table', msg + self.size_note(), parent=self.root)
        self.work('Rebuilding the size table for every mod...', self.be.rebuild_sizes, done)

    def open_mods(self):
        d = self.cfg['mods']
        if os.path.isdir(d):
            os.startfile(d)
        else:
            self.error('No mods folder', ToolError('The mods folder does not exist:\n%s\nSet it in Settings.' % d))

    def settings(self):
        if self.busy:
            return
        s = Settings(self.root, self.cfg)
        if s.changed:
            self.cfg.update(s.changed)
            save_config(self.cfg)
            self.be.apply()
            self.cache.clear()
            self.startup()


class ModPicker(simpledialog.Dialog):
    NEW = 'New mod...'

    def __init__(self, parent, mods):
        self.mods, self.result = mods, None
        super().__init__(parent, 'Import into which mod?')

    def body(self, f):
        ttk.Label(f, text='Put the new texture in this mod:').grid(row=0, column=0, columnspan=2, sticky='w')
        self.choice = tk.StringVar(value=self.mods[0] if self.mods else self.NEW)
        cb = ttk.Combobox(f, textvariable=self.choice, values=self.mods + [self.NEW], state='readonly', width=36)
        cb.grid(row=1, column=0, columnspan=2, sticky='we', pady=4)
        cb.bind('<<ComboboxSelected>>', lambda _: self.toggle())
        ttk.Label(f, text='New mod name:').grid(row=2, column=0, sticky='w')
        self.name = tk.StringVar()
        self.entry = ttk.Entry(f, textvariable=self.name, width=26)
        self.entry.grid(row=2, column=1, sticky='we')
        self.toggle()
        return cb

    def toggle(self):
        new = self.choice.get() == self.NEW
        self.entry.config(state='normal' if new else 'disabled')
        if new:
            self.entry.focus_set()

    def validate(self):
        if self.choice.get() == self.NEW and not self.name.get().strip():
            messagebox.showerror('Name needed', 'Type a name for the new mod.', parent=self)
            return False
        return True

    def apply(self):
        self.result = self.name.get().strip() if self.choice.get() == self.NEW else self.choice.get()


class Settings(simpledialog.Dialog):
    ROWS = [('romfs', 'Game romfs folder (read only)', True), ('mods', 'Mods folder', True),
            ('texconv_dir', 'Folder with texconv.exe', True), ('py_zstd', 'Python 3.14+ command (zstd)', False)]

    def __init__(self, parent, cfg):
        self.cfg, self.changed = cfg, None
        super().__init__(parent, 'Settings')

    def body(self, f):
        self.vars = {}
        for r, (k, label, is_dir) in enumerate(self.ROWS):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky='w', padx=(0, 6), pady=2)
            v = self.vars[k] = tk.StringVar(value=self.cfg[k])
            ttk.Entry(f, textvariable=v, width=56).grid(row=r, column=1, sticky='we')
            if is_dir:
                ttk.Button(f, text='...', width=3, command=lambda v=v: self.browse(v)).grid(row=r, column=2, padx=(4, 0))
        ttk.Label(f, text='Point "Mods folder" at a scratch folder to try things without touching the game.',
                  foreground='#555').grid(row=len(self.ROWS), column=0, columnspan=3, sticky='w', pady=(6, 0))
        ttk.Button(f, text='Use an NX2navi install...', command=self.use_nx2navi).grid(
            row=len(self.ROWS) + 1, column=0, sticky='w', pady=(6, 0))
        ttk.Label(f, text='Sets both folders to the install\'s romfs and mods (the folder with nx2navi.exe).',
                  foreground='#555').grid(row=len(self.ROWS) + 1, column=1, columnspan=2, sticky='w', pady=(6, 0))

    def use_nx2navi(self):
        d = filedialog.askdirectory(parent=self, title='The NX2navi install folder (the one with nx2navi.exe)')
        if not d:
            return
        d = os.path.normpath(d)
        if not os.path.exists(os.path.join(d, 'nx2navi.exe')):
            messagebox.showerror('Not an NX2navi install', 'nx2navi.exe is not in\n%s' % d, parent=self)
            return
        romfs = os.path.join(d, 'romfs')
        if not os.path.exists(os.path.join(romfs, 'Pack', 'ZsDic.pack.zs')):
            messagebox.showwarning('No romfs', 'The install has no romfs\\Pack\\ZsDic.pack.zs; set "Game romfs folder" '
                                   'to a full romfs of the same game version.', parent=self)
        else:
            self.vars['romfs'].set(romfs)
        os.makedirs(os.path.join(d, 'mods'), exist_ok=True)
        self.vars['mods'].set(os.path.join(d, 'mods'))

    def browse(self, v):
        d = filedialog.askdirectory(parent=self, initialdir=v.get() or None)
        if d:
            v.set(os.path.normpath(d))

    def apply(self):
        self.changed = {k: v.get().strip() for k, v in self.vars.items()}


def main():
    global MODS_OVERRIDE
    cfg = load_config()
    if '--mods' in sys.argv:   # e.g. a scratch folder for testing
        cfg['mods'] = MODS_OVERRIDE = sys.argv[sys.argv.index('--mods') + 1]
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # sharp text on scaled displays
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        from PIL import ImageTk  # noqa: F401
    except ImportError:
        root.withdraw()
        messagebox.showerror('Pillow missing', 'This needs Pillow for %s.\nIt runs with Python 3.12 (py -3.12).' % sys.executable)
        return
    App(root, cfg)
    root.mainloop()


if __name__ == '__main__':
    main()
