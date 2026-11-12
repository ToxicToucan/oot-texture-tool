# OOT Texture Tool

**Export and import textures for *The Legend of Zelda: Ocarina of Time* on Switch 2, to make texture mods. Works from a window, with no commands to type.**

![The texture tool window](docs/screenshot.png)

- **Find** any of the game's 20,000+ textures by typing part of its name.
- **Preview** the texture, with its format, size, mip levels and whether it can be imported.
- **Export** it as a PNG to edit in any paint program.
- **Import** your edited PNG into a mod folder. Every mip level is rebuilt from your image. BC7 and ASTC textures can be imported.

The tool only reads the game's files. It writes to your mod folder and nowhere else.

> This repository contains no game files. You need your own dump of the game.

---

## Contents

- [Install](#install)
- [Guide: your first texture mod](#guide-your-first-texture-mod)
- [Settings](#settings)
- [What can be imported](#what-can-be-imported)
- [Command line](#command-line)
- [Troubleshooting](#troubleshooting)
- [Building texconv.exe yourself](#building-texconvexe-yourself)
- [How it works](#how-it-works)
- [Credits and licence](#credits-and-licence)

---

## Install

You need Windows 10 or 11 (64-bit).

### 1. Install Python 3.12 and Pillow

The window runs on Python 3.12. Get it from [python.org](https://www.python.org/downloads/), and tick **"Add python.exe to PATH"** and **"py launcher"** during setup.

Then open a Command Prompt and install Pillow (the image library):

```bat
py -3.12 -m pip install pillow
```

### 2. Install Python 3.14 or later

The game's files are zstd-compressed. Only Python 3.14 and newer can unpack them without extra packages. Install it from [python.org](https://www.python.org/downloads/) as well; both versions can sit side by side.

The tool runs it as `py -3.15` by default. If you installed a different version, change the command in **Settings** (for example `py -3.14`).

### 3. Get the tool

Download this repository: click **Code > Download ZIP** at the top of the page and unzip it. Or clone it:

```bat
git clone https://github.com/ToxicToucan/oot-texture-tool.git
```

### 4. Add texconv.exe

`texconv.exe` is the small helper that encodes and decodes the texture formats.

1. Download `texconv-windows-x64.zip` from the [Releases](https://github.com/ToxicToucan/oot-texture-tool/releases) page.
2. Unzip it into the tool's folder, next to `texture_tool.py`.

It needs the Microsoft Visual C++ Redistributable (x64). Most PCs have it already; if not, get it from [Microsoft](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist).

Prefer to build it yourself? See [Building texconv.exe yourself](#building-texconvexe-yourself).

### 5. Start it

Double-click **`Texture Tool.cmd`**.

The first time, open **Settings...** and point it at your game's `romfs` folder and your mods folder. The window checks everything when it starts and tells you if something is missing.

---

## Guide: your first texture mod

As an example, this guide recolours adult Link's hair.

1. **Find the texture.** Type `hairs adult` in the search box. Words can be in any order. Click `Mt_Hairs_Player_Adult_Alb`.
2. **Check it.** The panel shows `Format: BC7`, the size (768 x 768) and **"Can be imported: yes"**.
3. **Export.** Click **Export PNG...** and save it somewhere handy, such as your Desktop.
4. **Edit.** Open the PNG in your paint program (GIMP, Paint.NET, Photoshop, Krita...) and make your changes.
   - **Keep the same width and height.**
   - **Keep the transparency.** See-through areas (the checkerboard in the preview) matter.
   - Save as PNG.
5. **Import.** Back in the tool, with the same texture selected, click **Import PNG...**.
   1. Pick your edited PNG.
   2. Choose a mod, or choose **New mod...** and type a name such as `Pink hair`.
   3. Wait a few seconds while it encodes every mip level.
6. **Turn it on.** Open the game's **Mods** tab, turn your mod on, then restart the game.
   - If the tool made the **`~Size table (made by the texture tool)`** mod (see [ASTC textures and the size table](#astc-textures-and-the-size-table)), turn that on too, and keep it last.

Your mod is a normal folder you can zip and share:

```
mods\
  Pink hair\
    romfs\
      Tex\
        Mt_Hairs_Player_Adult_Alb.bntx.zs
```

**Open mods folder** (top right of the window) opens it in Explorer.

If your mod includes an ASTC texture, also share the `~Size table (made by the texture tool)` folder, or tell people to click **Rebuild size table** after installing it.

### Texture name tips

| Ending | What it is |
| --- | --- |
| `_Alb` | Albedo: the colour you see. This is usually the one to edit. |
| `_Nrm` | Normal map (bumps). Looks purple and blue; edit with care. |
| `_AO`, `_Rgh`, `_Mtl`, `_Thc` | Shading data (shadowing, roughness and so on), not colour. |
| `_Gn0`, `_Gn3` ... | Gradient and mask data. |

`Player_Child_` and `Player_Adult_` are young and adult Link.

---

## Settings

| Setting | What it is | Default |
| --- | --- | --- |
| Game romfs folder | The game's `romfs` folder. It must contain `Tex\` and `Pack\ZsDic.pack.zs`. Only read. | `C:\Switch2\oot-recomp-probe\romfs` |
| Mods folder | Where mods are written. | `C:\Switch2\oot-recomp-probe\merged\mods` |
| Folder with texconv.exe | Where `texconv.exe` and its DLLs are. | The tool's own folder |
| Python 3.14+ command | Runs the zstd step. | `py -3.15` |

The settings are saved in `%LOCALAPPDATA%\oot-texture-gui.json`.

**To experiment safely,** point **Mods folder** at an empty test folder. You can also start the tool with a test folder for one run only:

```bat
pyw -3.12 texture_gui.pyw --mods "C:\some\test\folder"
```

The tool refuses any mods folder that overlaps the game's romfs, `merged\bin` or `merged\work`.

---

## What can be imported

| Format | Export | Import |
| --- | --- | --- |
| BC7 | Yes | **Yes** |
| ASTC (all block sizes) | Yes | **Yes**, saved as BC7 |
| BC1 to BC6H | Yes | Not yet |

The panel tells you which applies when you pick a texture.

Quality: exporting, importing unchanged and exporting again changes the image by an amount you can't see. That's PSNR 56.7 dB for BC7 (`Mt_Hairs_Player_Adult_Alb`) and 82.0 dB for ASTC (`Player_Adult_Hair_Alb`).

### ASTC textures and the size table

The game stores many textures as ASTC, adult Link's hair (`Player_Adult_Hair_Alb`) for example. The tool can't write ASTC, so the mod's copy is rebuilt as BC7. It looks the same, but the file is bigger.

The game keeps a **resource size table** listing how much memory each file needs, and a bigger texture needs a bigger entry. So after every import the tool also writes a copy of that table, with every entry your mods need raised, as its own mod:

```
mods\
  ~Size table (made by the texture tool)\
    romfs\System\Resource\ResourceSizeTable....rsizetable.zs
```

- **Keep it on, and last in the load order.** Its name starts with `~` so it sorts last.
- It covers **all** the mods in the folder, not just the last one you imported.
- It isn't shown in the import list, and nothing else can be imported into it.
- If you delete a mod or copy one in by hand, click **Rebuild size table** (top right). If no mod needs a bigger entry, the table is removed.
- Only BC7 imports never need it. The game's own size table is never changed.

![An ASTC texture in the window](docs/screenshot-astc.png)

ASTC import is new, so check your texture in game.

---

## Command line

The window drives `texture_tool.py`, which you can also run directly:

```bat
py -3.12 texture_tool.py export <romfs>\Tex\Player_Child_Face_Alb.bntx.zs face.png
py -3.12 texture_tool.py import <romfs>\Tex\Player_Child_Face_Alb.bntx.zs face.png "<mods>\My face mod"
py -3.12 texture_tool.py sizes <mods>
```

`sizes` rebuilds the size table mod, the same as the **Rebuild size table** button.

Environment variables: `TEXCONV_DIR` (folder with `texconv.exe`), `PY_ZSTD` (the Python 3.14+ command), and `OOT_ROMFS` (the game romfs, if the texture is not inside one).

---

## Troubleshooting

| Message | Fix |
| --- | --- |
| *texconv.exe was not found* | Put `texconv.exe` and its three `astcdec-*.dll` files next to `texture_tool.py`, or set the folder in Settings. |
| *"py -3.15" did not run Python 3.14+* | Install Python 3.14 or later, or change the command in Settings to the version you have. |
| *The game textures folder was not found* | Set **Game romfs folder** to the folder that contains `Tex` and `Pack`. |
| *Wrong PNG size* | Resize the PNG to exactly the size shown in the panel. Editing the exported PNG keeps it right. |
| *cannot be imported yet* | The texture is BC1 to BC6H. See [What can be imported](#what-can-be-imported). |
| *Pillow missing* | Run `py -3.12 -m pip install pillow`. |
| Nothing happens on double-click | Make sure Python 3.12 is installed with the py launcher, then try `py -3.12 texture_gui.pyw` in a Command Prompt to see the error. |
| My change doesn't show in game | Turn the mod on in the **Mods** tab and restart the game. A mod later in the load order wins if two change the same file. |
| The game crashes or the texture is missing after an ASTC import | Make sure `~Size table (made by the texture tool)` is on and last, then click **Rebuild size table**. |

---

## Building texconv.exe yourself

You need Visual Studio 2022 or later with the C++ tools, [LLVM](https://github.com/llvm/llvm-project/releases) (`clang-cl`), CMake, Ninja and PowerShell 7.

```bat
powershell -File textures\scripts\fetch_third_party.ps1
pwsh -File textures\scripts\build.ps1
build-texconv.cmd
```

1. The first command fetches the pinned versions of astc-encoder, bc7enc_rdo and ISPC into `textures\third_party`. The exact versions are listed in [textures/THIRD_PARTY.md](textures/THIRD_PARTY.md).
2. The second builds the `astc2bc7` library into `textures\build\release`.
3. The third builds `texconv.exe` next to the tool and copies the DLLs it needs.

If LLVM is not in `C:\Program Files\LLVM`, run `set LLVM_DIR=<your LLVM folder>` first.

---

## How it works

- The game's textures are `.bntx.zs` files: Nintendo BNTX texture containers, zstd-compressed with dictionaries stored in the game's own `Pack\ZsDic.pack.zs`. `zs_helper.py` handles that step on Python 3.14+.
- Texture data on the console is stored in GPU "block-linear" order: GOBs of 64 bytes by 8 rows, stacked in blocks. The order of bytes *inside* a GOB on Switch 2 differs from the original Switch. It was found by testing all orders against the game's own textures; see `swizzle_offsets` in `texture_tool.py`.
- `texconv.exe` decodes ASTC (ARM astcenc) and encodes BC7 (Binomial bc7e).
- On import, every mip level is downscaled from your PNG, encoded as BC7, put back into block-linear order, and recompressed with the same dictionary. A BC7 texture keeps its exact size.
- An ASTC texture's container is laid out again for BC7: new mip offsets, then the data block, relocation table and header pointers are moved. The resource size table (CRC32 of each file's path, mapped to the size it needs: unpacked size + 280 for textures) is merged for every mod into the size table mod.

| File | Purpose |
| --- | --- |
| `Texture Tool.cmd` | Double-click launcher |
| `texture_gui.pyw` | The window (Tkinter) |
| `texture_tool.py` | Export and import; also a command-line tool |
| `zs_helper.py` | zstd with the game's dictionaries |
| `texconv.cpp`, `build-texconv.cmd` | The codec helper and its build script |
| `textures/` | The `astc2bc7` library that `texconv` links (ASTC decode, BC7 encode) |

---

## Credits and licence

- This tool is licensed under the **GNU GPL v3.0 or later**; see [LICENSE](LICENSE).
- [ARM astc-encoder](https://github.com/ARM-software/astc-encoder) (Apache-2.0) decodes ASTC.
- [bc7enc_rdo / bc7e](https://github.com/richgel999/bc7enc_rdo) by Richard Geldreich, Jr. and Binomial LLC (Apache-2.0, plus MIT or public domain for the rest) encodes BC7.
- [Intel ISPC](https://github.com/ispc/ispc) (BSD-3-Clause) compiles bc7e.
- The BNTX field layout follows the notes in [Switch-Toolbox](https://github.com/KillzXGaming/Switch-Toolbox).

The licence texts for the third-party parts are in [textures/licenses](textures/licenses).

*Not affiliated with or endorsed by Nintendo. The Legend of Zelda and Ocarina of Time are trademarks of Nintendo. This repository contains no game code or assets; use it only with a game you own.*
