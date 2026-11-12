# SPDX-License-Identifier: GPL-3.0-or-later
# The texture tool's zstd step (needs Python 3.14+ for compression.zstd), with the game's dictionaries (Pack/ZsDic.pack.zs):
#   zs_helper.py d <romfs> <in.zs> <out.bin>          decompress; prints the index of the dictionary used (-1: none)
#   zs_helper.py c <romfs> <in.bin> <out.zs> <dict>   compress with that dictionary index (-1: none)
#   zs_helper.py D <romfs> <list.txt> <unused>        decompress many: each line of list.txt is <in.zs>TAB<out.bin>
import os, struct, sys
from compression import zstd

def sarc_files(data):
    hdr_len = struct.unpack_from('<H', data, 4)[0]
    data_off = struct.unpack_from('<I', data, 12)[0]
    count = struct.unpack_from('<H', data, hdr_len + 6)[0]
    nodes = [struct.unpack_from('<IIII', data, hdr_len + 12 + i * 16) for i in range(count)]
    names_off = hdr_len + 12 + count * 16 + 8
    return [data[data_off + s:data_off + e] for h, a, s, e in nodes]

mode, romfs, src, dst = sys.argv[1:5]
dicts = [zstd.ZstdDict(d) for d in sarc_files(zstd.decompress(open(os.path.join(romfs, 'Pack', 'ZsDic.pack.zs'), 'rb').read()))]
if mode == 'D':   # many files in one run (the Levels tab loads hundreds of models)
    by_id = {d.dict_id: d for d in dicts}
    for line in open(src, encoding='utf-8').read().splitlines():
        a, _, b = line.partition(chr(9))
        try:
            raw = open(a, 'rb').read()
            d = by_id.get(zstd.get_frame_info(raw).dictionary_id)
            open(b, 'wb').write(zstd.decompress(raw, zstd_dict=d) if d else zstd.decompress(raw))
        except (OSError, zstd.ZstdError) as e:
            print('%s: %s' % (a, e), file=sys.stderr)
    sys.exit(0)
raw = open(src, 'rb').read()
if mode == 'd':
    # A frame made without a dictionary also decodes with one, so try none first: such files (the resource size table, read
    # at boot before the dictionaries are loaded) must be written back without a dictionary.
    try:
        open(dst, 'wb').write(zstd.decompress(raw))
        print(-1)
        sys.exit(0)
    except zstd.ZstdError:
        pass
    for i, d in enumerate(dicts):
        try:
            open(dst, 'wb').write(zstd.decompress(raw, zstd_dict=d))
            print(i)
            break
        except zstd.ZstdError:
            pass
    else:
        open(dst, 'wb').write(zstd.decompress(raw))
        print(-1)
else:
    k = int(sys.argv[5])
    open(dst, 'wb').write(zstd.compress(raw, zstd_dict=dicts[k]) if k >= 0 else zstd.compress(raw))
