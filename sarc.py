# SPDX-License-Identifier: GPL-3.0-or-later
"""A small read-only SARC (.pack) reader: returns {path: bytes}."""
import struct


def parse(b):
    if b[:4] != b'SARC':
        raise ValueError('not a SARC file')
    hdr_len, bom = struct.unpack_from('<HH', b, 4)
    e = '<' if bom == 0xFEFF else '>'
    data_off = struct.unpack_from(e + 'I', b, 12)[0]
    o = hdr_len                                   # SFAT
    sfat_len, count = struct.unpack_from(e + 'HH', b, o + 4)
    nodes = o + sfat_len
    sfnt = nodes + 16 * count
    names = sfnt + struct.unpack_from(e + 'H', b, sfnt + 4)[0]
    out = {}
    for i in range(count):
        _, attr, start, end = struct.unpack_from(e + '4I', b, nodes + 16 * i)
        if attr & 0x01000000:
            n = names + (attr & 0xFFFF) * 4
            name = b[n:b.index(b'\0', n)].decode('utf-8', 'replace')
        else:
            name = '%08x' % i
        out[name] = b[data_off + start:data_off + end]
    return out
