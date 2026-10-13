import struct, re
def table_directory(d, lo=0, hi=None):
    """Return [(name, offset, cols, rows)] from an SGBD table directory.
    Entry layout: [offset:u32 LE][0:u32][cols:u32][rows:u32][name:NUL-terminated]"""
    hi = hi or len(d)
    ents, seen = [], set()
    for m in re.finditer(rb'[A-Za-z_][A-Za-z0-9_]{3,60}\x00', d[lo:hi]):
        s = lo + m.start()
        if s < 16: continue
        off, unk, cols, rows = struct.unpack_from('<IIII', d, s-16)
        if unk or not (0 < cols < 20) or not (0 < rows < 20000) or not (0 < off < len(d)):
            continue
        if off in seen: continue
        seen.add(off)
        ents.append((m.group()[:-1].decode('latin-1'), off, cols, rows))
    return ents

def read_table(d, off, cols, rows):
    p, fields, need = off, [], cols*(rows+1)
    while len(fields) < need and p < len(d):
        e = d.find(b'\x00', p)
        if e < 0: break
        fields.append(d[p:e].decode('latin-1')); p = e+1
    return fields[:cols], [fields[i:i+cols] for i in range(cols, len(fields), cols)]
