# SPDX-License-Identifier: GPL-3.0-or-later
"""A small read-only BYML reader (versions 2-7, little endian), enough for the game's level layouts (*.bcett.byml)."""
import struct


def parse(b):
    if b[:2] != b'YB':
        raise ValueError('not a little-endian BYML file')
    keys_off, strs_off, root_off = struct.unpack_from('<3I', b, 4)

    def u32(o):
        return struct.unpack_from('<I', b, o)[0]

    def table(o):
        if not o:
            return []
        n = u32(o) >> 8
        offs = struct.unpack_from('<%dI' % (n + 1), b, o + 4)
        return [b[o + offs[i]:o + offs[i + 1]].split(b'\0', 1)[0].decode('utf-8', 'replace') for i in range(n)]

    keys, strs = table(keys_off), table(strs_off)

    def value(t, v):
        if t == 0xA0:
            return strs[v]
        if t in (0xC0, 0xC1, 0x20, 0x21):
            return node(v)
        if t == 0xA1:
            return b[v + 4:v + 4 + u32(v)]
        if t == 0xD0:
            return bool(v)
        if t == 0xD1:
            return struct.unpack('<i', struct.pack('<I', v))[0]
        if t == 0xD2:
            return struct.unpack('<f', struct.pack('<I', v))[0]
        if t == 0xD3:
            return v
        if t == 0xD4:
            return struct.unpack_from('<q', b, v)[0]
        if t == 0xD5:
            return struct.unpack_from('<Q', b, v)[0]
        if t == 0xD6:
            return struct.unpack_from('<d', b, v)[0]
        if t == 0xFF:
            return None
        raise ValueError('BYML node type 0x%x' % t)

    def node(o):
        t, n = b[o], u32(o) >> 8
        if t == 0xC1:
            out = {}
            for i in range(n):
                e = o + 4 + 8 * i
                k = u32(e) & 0xFFFFFF
                out[keys[k]] = value(b[e + 3], u32(e + 4))
            return out
        if t == 0xC0:
            types = b[o + 4:o + 4 + n]
            vals = o + 4 + ((n + 3) & ~3)
            return [value(types[i], u32(vals + 4 * i)) for i in range(n)]
        raise ValueError('BYML container type 0x%x' % t)

    return node(root_off) if root_off else None
