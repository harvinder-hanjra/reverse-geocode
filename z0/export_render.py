#!/usr/bin/env python3
"""
export_render.py — Export simplified rendering GeoJSON from z0_prep.bin.

Reads the prep binary, merges polygon parts by admin_id, simplifies
geometries, and writes a GeoJSON FeatureCollection with:
  - feature.id = admin_id  (integer, for MapLibre setFeatureState)
  - feature.properties = { country, adm1, adm2 }
  - feature.geometry = simplified (Multi)Polygon

Usage:
    python export_render.py [z0_prep.bin] [output.geojson] [--tolerance 0.05]
"""

import json
import struct
import sys
import time
from collections import defaultdict

import zstandard as zstd
from shapely.geometry import mapping, Polygon, MultiPolygon
from shapely.ops import unary_union

MAGIC = b"Z0PREP01"
DEFAULT_TOLERANCE = 0.05  # degrees (~5.5 km at equator)


def read_prep(path):
    print(f"Reading {path} …")
    with open(path, "rb") as f:
        raw = f.read()

    if raw[:8] != MAGIC:
        raise ValueError(f"Bad magic: {raw[:8]!r}")

    pos = 8
    num_admins, num_polys, name_zstd_len, admin_table_len = struct.unpack_from("<IIII", raw, pos)
    pos += 16

    print(f"  {num_admins} admins, {num_polys} polygon parts")

    # Decompress name tables
    name_zstd = raw[pos:pos + name_zstd_len]
    pos += name_zstd_len
    dctx = zstd.ZstdDecompressor()
    tables = json.loads(dctx.decompress(name_zstd).decode("utf-8"))
    countries = tables["countries"]
    adm1s = tables["adm1s"]
    adm2s = tables["adm2s"]

    # Admin lookup table: [country_idx:u8, adm1_idx:u16, adm2_idx:u16] × num_admins
    admin_table = []
    for i in range(num_admins):
        c_idx, a1_idx, a2_idx = struct.unpack_from("<BHH", raw, pos + i * 5)
        admin_table.append((c_idx, a1_idx, a2_idx))
    pos += admin_table_len

    # Polygon stream — group parts by admin_id
    polys_by_admin = defaultdict(list)
    for i in range(num_polys):
        admin_id, num_rings = struct.unpack_from("<HI", raw, pos)
        pos += 6
        rings = []
        for _ in range(num_rings):
            num_pts, = struct.unpack_from("<I", raw, pos)
            pos += 4
            coords = struct.unpack_from(f"<{num_pts * 2}f", raw, pos)
            pos += num_pts * 8
            # coords is interleaved (lon, lat, lon, lat, ...)
            pts = [(coords[j], coords[j + 1]) for j in range(0, len(coords), 2)]
            rings.append(pts)
        if rings:
            try:
                ext = rings[0]
                holes = rings[1:]
                poly = Polygon(ext, holes)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if not poly.is_empty:
                    polys_by_admin[admin_id].append(poly)
            except Exception:
                pass
        if (i + 1) % 5000 == 0:
            print(f"  Read {i + 1}/{num_polys} parts …", end="\r")
    print(f"  Read {num_polys}/{num_polys} parts.    ")

    return admin_table, countries, adm1s, adm2s, polys_by_admin


def build_geojson(admin_table, countries, adm1s, adm2s, polys_by_admin, tolerance):
    features = []
    total = len(polys_by_admin)
    t0 = time.time()

    for n, (admin_id, parts) in enumerate(sorted(polys_by_admin.items())):
        if (n + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (n + 1) / elapsed
            remaining = (total - n - 1) / rate if rate > 0 else 0
            print(f"  Simplifying {n + 1}/{total}  ({remaining:.0f}s left) …", end="\r")

        # Merge all parts for this admin region
        if len(parts) == 1:
            geom = parts[0]
        else:
            geom = unary_union(parts)
            if not geom.is_valid:
                geom = geom.buffer(0)

        # Simplify
        simplified = geom.simplify(tolerance, preserve_topology=True)
        if simplified.is_empty:
            simplified = geom  # fallback: keep original if simplification kills it

        c_idx, a1_idx, a2_idx = admin_table[admin_id]
        features.append({
            "type": "Feature",
            "id": admin_id,
            "properties": {
                "country": countries[c_idx],
                "adm1":    adm1s[a1_idx],
                "adm2":    adm2s[a2_idx],
            },
            "geometry": mapping(simplified),
        })

    print(f"  Simplified {total}/{total}.              ")
    return {"type": "FeatureCollection", "features": features}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("prep", nargs="?", default="z0_prep.bin")
    ap.add_argument("out",  nargs="?", default="../ui/public/data/adm2_render.geojson")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                    help="Simplification tolerance in degrees (default 0.05 ≈ 5.5 km)")
    args = ap.parse_args()

    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    admin_table, countries, adm1s, adm2s, polys_by_admin = read_prep(args.prep)

    print(f"Building GeoJSON (tolerance={args.tolerance}°) …")
    fc = build_geojson(admin_table, countries, adm1s, adm2s, polys_by_admin, args.tolerance)

    print(f"Writing {args.out} …")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(fc, f, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(args.out)
    print(f"Done: {args.out} = {size / 1024 / 1024:.1f} MB  ({len(fc['features'])} features)")


if __name__ == "__main__":
    main()
