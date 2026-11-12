#! python3.12
# SPDX-License-Identifier: GPL-3.0-or-later
"""A window for texture_tool.py: find a game texture, preview it, export it as a PNG, import an edited PNG into a mod.

Double-click "Texture Tool.cmd" (next to this file), or run:  py -3.12 texture_gui.pyw
The folders (game romfs, mods, texconv) are set in Settings and kept in %LOCALAPPDATA%\\oot-texture-gui.json.
It never writes to the game's romfs, merged\\bin or merged\\work: imports go only to <mods>\\<mod name>\\romfs\\Tex\\.
"""
import io
import json
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

    def decode(self, name):
        """(info dict, mip 0 as a PIL RGBA image) for a texture; the same decoding as texture_tool export."""
        from PIL import Image
        src = self.path(name)
        romfs = tt.game_romfs(tt.find_romfs(src))
        with tempfile.TemporaryDirectory() as tmp:
            d, _ = tt.unzs(src, romfs, tmp)
            t = tt.Texture(d)
            w, h, wb, hb = t.mip_dims(0)
            blocks = tt.read_mip_blocks(t, 0)
            if t.kind in tt.ASTC_BLOCKS:
                bp, rp = os.path.join(tmp, 'b.bin'), os.path.join(tmp, 'p.rgba')
                open(bp, 'wb').write(blocks)
                tt.run('decode-astc', bp, rp, w, h, t.bw, t.bh, 'srgb' if t.srgb else 'linear')
                img = Image.frombytes('RGBA', (w, h), open(rp, 'rb').read())
            else:
                dxgi = tt.BC[t.kind][2] + (1 if t.srgb and t.kind in (0x1a, 0x1b, 0x1c, 0x20) else 0)
                hdr = b'DDS ' + struct.pack('<7I44x', 124, 0x1007, h, w, len(blocks), 0, 1) + struct.pack('<2I4s20x', 32, 4, b'DX10')
                hdr += struct.pack('<5I', 0x1000, 0, 0, 0, 0) + struct.pack('<5I', dxgi, 3, 0, 1, 0)
                img = Image.open(io.BytesIO(hdr + blocks)).convert('RGBA')
                img.load()
        astc = t.kind in tt.ASTC_BLOCKS
        info = {'name': name, 'w': t.w, 'h': t.h, 'mips': t.mips, 'srgb': t.srgb,
                'format': ('ASTC %dx%d' % (t.bw, t.bh)) if astc else tt.BC[t.kind][0],
                'importable': t.kind == 0x20 or (astc and ASTC_IMPORT), 'astc': astc}
        if not info['importable']:
            info['why_not'] = ('ASTC textures export fine but this version cannot import them.' if astc else
                               '%s textures cannot be imported yet; only BC7%s can.' % (info['format'], ' and ASTC' if ASTC_IMPORT else ''))
        return info, img

    def export(self, name, png):
        try:
            tt.export_png(self.path(name), png)
        except TOOL_ERRORS as e:
            raise ToolError(str(e))

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
        order, off = [], []
        try:
            for line in open(os.path.join(self.cfg['mods'], 'mods.ini'), encoding='utf-8'):
                k, _, v = line.partition('=')
                items = [x.strip() for x in v.split(',') if x.strip()]
                if k.strip() == 'order':
                    order = items
                elif k.strip() == 'off':
                    off = items
        except OSError:
            pass
        return 'off' if mod in off else 'on' if mod in order else 'not listed'

    def safe_mod_dir(self, mod):
        bad = set('<>:"/\\|?*')
        if not mod or mod.strip() != mod or mod in ('.', '..') or any(c in bad or ord(c) < 32 for c in mod) or mod.endswith('.'):
            raise ToolError('"%s" is not a usable folder name. Avoid  < > : " / \\ | ? *  and leading or trailing spaces.' % mod)
        if ',' in mod:
            raise ToolError('Mod names cannot contain a comma (mods.ini uses commas between names).')
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
    def __init__(self, root, cfg):
        self.root, self.cfg = root, cfg
        self.be = Backend(cfg)
        self.names, self.shown = [], []
        self.info, self.img, self.photo = None, None, None
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
        e = ttk.Entry(top, textvariable=self.q, width=40)
        e.pack(side='left', padx=(4, 8))
        e.focus_set()
        self.count = ttk.Label(top, text='')
        self.count.pack(side='left')
        ttk.Button(top, text='Settings...', command=self.settings).pack(side='right')
        ttk.Button(top, text='Open mods folder', command=self.open_mods).pack(side='right', padx=4)
        if SIZE_MOD:
            ttk.Button(top, text='Rebuild size table', command=self.rebuild_sizes).pack(side='right')

        body = ttk.PanedWindow(root, orient='horizontal')
        body.pack(fill='both', expand=True, padx=8, pady=8)
        left = ttk.Frame(body)
        self.lb = tk.Listbox(left, activestyle='none', exportselection=False, font=('Segoe UI', 9))
        sb = ttk.Scrollbar(left, orient='vertical', command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.lb.bind('<<ListboxSelect>>', lambda _: self.select())
        body.add(left, weight=1)

        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        self.title = ttk.Label(right, text='Pick a texture on the left.', font=('Segoe UI', 11, 'bold'))
        self.title.pack(anchor='w')
        self.details = ttk.Label(right, text='', justify='left')
        self.details.pack(anchor='w')
        self.canimport = ttk.Label(right, text='', justify='left', wraplength=int(PREVIEW * f))
        self.canimport.pack(anchor='w', pady=(2, 6))
        btns = ttk.Frame(right)
        btns.pack(anchor='w', pady=(0, 6))
        self.bexp = ttk.Button(btns, text='Export PNG...', command=self.export, state='disabled')
        self.bexp.pack(side='left')
        self.bimp = ttk.Button(btns, text='Import PNG...', command=self.import_, state='disabled')
        self.bimp.pack(side='left', padx=6)
        self.canvas = tk.Canvas(right, width=int(PREVIEW * f), height=int(PREVIEW * f), highlightthickness=1,
                                highlightbackground='#aaa', bg='#ddd')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _: self.draw())
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
            self.filter()
            if err or bad:
                self.error('Something is missing', err or ToolError('\n\n'.join(bad)))
            else:
                self.say('Ready. %d textures in %s. Mods go to %s' % (len(self.names), self.be.tex_dir, self.cfg['mods']))
        self.work('Checking the tools...', self.be.check_tools, done)

    def filter(self):
        words = self.q.get().lower().split()
        self.shown = [n for n in self.names if all(w in n.lower() for w in words)]
        self.lb.delete(0, 'end')
        self.lb.insert('end', *[n[:-8] for n in self.shown])
        self.count.config(text='%d of %d' % (len(self.shown), len(self.names)))

    def select(self):
        sel = self.lb.curselection()
        if not sel:
            return
        name = self.shown[sel[0]]
        self.want = name
        if name in self.cache:
            self.show(*self.cache[name])
            return
        if self.busy:
            return  # picked up when the current job ends

        def done(res, err):
            if err:
                self.show_error(name, err)
            else:
                self.cache[name] = res
                if len(self.cache) > 40:
                    self.cache.pop(next(iter(self.cache)))
                if self.want == name:
                    self.show(*res)
            if self.want != name:
                self.select()
        self.work('Reading %s...' % name, lambda: self.be.decode(name), done)

    def show_error(self, name, err):
        self.info, self.img = None, None
        self.canvas.delete('all')
        self.title.config(text=name[:-8])
        self.details.config(text='Could not read this texture.')
        self.canimport.config(text=str(err), foreground='#a00')
        self.bexp.config(state='disabled')
        self.bimp.config(state='disabled')
        self.say('Could not read %s' % name)

    def show(self, info, img):
        self.info, self.img = info, img
        scale = self.draw()
        self.title.config(text=info['name'][:-8])
        self.details.config(text='Format: %s%s\nSize: %d x %d\nMip levels: %d%s' % (
            info['format'], ' (sRGB)' if info['srgb'] else '', info['w'], info['h'], info['mips'],
            '' if scale == 1 else '   (preview at %d%%)' % round(scale * 100)))
        if info['importable'] and info['astc']:
            self.canimport.config(text='Can be imported: yes. The mod\'s copy is saved as BC7 (a bigger file); the tool '
                                       'raises the game\'s size table to match, in the mod "%s".' % SIZE_MOD, foreground='#060')
        elif info['importable']:
            self.canimport.config(text='Can be imported: yes (BC7).', foreground='#060')
        else:
            self.canimport.config(text='Can be imported: not yet. ' + info['why_not'], foreground='#a50')
        self.bexp.config(state='normal')
        self.bimp.config(state='normal')
        self.say('%s: %s, %d x %d, %d mip levels' % (info['name'], info['format'], info['w'], info['h'], info['mips']))

    def draw(self):
        """Draw the image to fit the preview area (on a checkerboard, so see-through parts show); returns the scale."""
        from PIL import Image, ImageTk
        img = self.img
        if img is None or self.info is None:
            return 1
        cw, ch = max(16, self.canvas.winfo_width() - 4), max(16, self.canvas.winfo_height() - 4)
        scale = min(cw / img.width, ch / img.height)
        if scale > 1:
            scale = max(1, int(scale))   # whole steps when enlarging small textures
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        view = img.resize(size, Image.NEAREST if scale >= 1 else Image.LANCZOS)
        board = Image.new('RGBA', size, (200, 200, 200, 255))
        dark = Image.new('RGBA', (16, 16), (150, 150, 150, 255))
        for y in range(0, size[1], 16):
            for x in range((y // 16) % 2 * 16, size[0], 32):
                board.paste(dark, (x, y))
        board.alpha_composite(view)
        self.photo = ImageTk.PhotoImage(board)
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.photo)
        return scale

    # --- buttons
    def export(self):
        info = self.info
        if not info or self.busy:
            return
        png = filedialog.asksaveasfilename(parent=self.root, title='Export PNG', initialdir=self.cfg['last_dir'],
                                           initialfile=info['name'][:-8] + '.png', defaultextension='.png',
                                           filetypes=[('PNG image', '*.png')])
        if not png:
            return
        self.cfg['last_dir'] = os.path.dirname(png)
        save_config(self.cfg)

        def done(_, err):
            if err:
                self.error('Export failed', err)
            else:
                self.say('Exported %s -> %s' % (info['name'], png))
                messagebox.showinfo('Exported', 'Saved %d x %d PNG:\n%s\n\nEdit it, keep the same size, then use Import PNG.'
                                    % (info['w'], info['h'], png), parent=self.root)
        self.work('Exporting %s...' % info['name'], lambda: self.be.export(info['name'], png), done)

    def import_(self):
        info = self.info
        if not info or self.busy:
            return
        if not info['importable']:
            self.error('Cannot import into this texture', ToolError('%s is %s.\n\n%s' % (info['name'], info['format'], info['why_not'])))
            return
        png = filedialog.askopenfilename(parent=self.root, title='Pick the edited PNG for %s (%d x %d)' % (info['name'][:-8], info['w'], info['h']),
                                         initialdir=self.cfg['last_dir'], filetypes=[('PNG image', '*.png')])
        if not png:
            return
        self.cfg['last_dir'] = os.path.dirname(png)
        save_config(self.cfg)
        try:
            self.be.check_import(info, png)
        except ToolError as e:
            self.error('Cannot import this PNG', e)
            return
        mod = ModPicker(self.root, self.be.mods()).result
        if not mod:
            return
        try:
            dst = os.path.join(self.be.safe_mod_dir(mod), 'romfs', 'Tex', info['name'])
        except ToolError as e:
            self.error('Cannot use that mod folder', e)
            return
        if os.path.exists(dst) and not messagebox.askyesno(
                'Replace?', 'The mod "%s" already has %s.\nReplace it?' % (mod, info['name']), parent=self.root):
            return

        def done(path, err):
            if err:
                self.error('Import failed', err)
                return
            state = self.be.mod_state(mod)
            how = {'on': 'It is already on in the Mods tab; restart the game to see it.',
                   'off': 'It is turned OFF: turn it on in the game\'s Mods tab, then restart the game.'}.get(
                state, 'Turn it on in the game\'s Mods tab (menu), then restart the game.')
            self.say('Imported %s into mod "%s"' % (info['name'], mod))
            messagebox.showinfo('Imported', 'Done. Wrote:\n%s\n\n%s%s' % (path, how, self.size_note()), parent=self.root)
        self.work('Importing into %s (encoding every mip level, this can take a little while)...' % mod,
                  lambda: self.be.do_import(info, png, mod), done)

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
