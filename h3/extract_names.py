#!/usr/bin/env python3
"""Extract the zstd-compressed name table from h3_geo.bin to h3_names.json."""
import json, mmap, struct, sys
import zstandard as zstd

bin_path  = sys.argv[1] if len(sys.argv) > 1 else "h3_geo.bin"
out_path  = sys.argv[2] if len(sys.argv) > 2 else "h3_names.json"

with open(bin_path, "rb") as f:
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    name_off = struct.unpack_from("<I", mm, 16)[0]
    compressed = bytes(mm[name_off:])

names = json.loads(zstd.ZstdDecompressor().decompress(compressed))
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(names, f, ensure_ascii=False, separators=(",", ":"))
print(f"Done: {len(names['countries'])} countries → {out_path}")
