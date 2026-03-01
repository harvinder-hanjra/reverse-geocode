#!/usr/bin/env python3
"""
make_render_geojson.py — Build adm2_render.geojson from z0_prep_gadm_full.bin.

Each feature has:
  id:         admin_id (integer, matches z0.lookup() output)
  properties: {country, adm1, adm2}
  geometry:   simplified Polygon or MultiPolygon

Usage:
    python make_render_geojson.py [z0_prep.bin] [output.geojson]
"""

import json
import os
import struct
import sys

import zstandard as zstd
from shapely.geometry import Polygon, MultiPolygon, mapping
from shapely.ops import unary_union

try:
    from tqdm import tqdm
    def progress(it, **kw): return tqdm(it, **kw)
except ImportError:
    def progress(it, total=None, desc='', **kw):
        n = 0
        for item in it:
            yield item
            n += 1
            if n % 50000 == 0:
                print(f"  {desc}: {n:,}/{total or '?'}", end='\r', flush=True)
        print()

SIMPLIFY_TOL = 0.01   # degrees (~1 km)
COORD_DIGITS = 5      # round output coords to 5 decimal places (~1 m)


def round_geom(obj):
    """Recursively round all coordinates in a GeoJSON geometry dict."""
    t = obj["type"]
    if t == "Polygon":
        obj["coordinates"] = [
            [[round(x, COORD_DIGITS), round(y, COORD_DIGITS)] for x, y in ring]
            for ring in obj["coordinates"]
        ]
    elif t == "MultiPolygon":
        obj["coordinates"] = [
            [
                [[round(x, COORD_DIGITS), round(y, COORD_DIGITS)] for x, y in ring]
                for ring in poly
            ]
            for poly in obj["coordinates"]
        ]
    return obj


def main():
    prep_path = sys.argv[1] if len(sys.argv) > 1 else "z0/z0_prep_gadm_full.bin"
    out_path  = sys.argv[2] if len(sys.argv) > 2 else "ui/public/data/adm2_render.geojson"

    print(f"Reading {prep_path} …")
    with open(prep_path, "rb") as f:
        raw = f.read()

    pos = 8  # skip magic
    num_admins    = struct.unpack_from('<I', raw, pos)[0]; pos += 4
    num_polys     = struct.unpack_from('<I', raw, pos)[0]; pos += 4
    name_zstd_len = struct.unpack_from('<I', raw, pos)[0]; pos += 4
    admin_tbl_len = struct.unpack_from('<I', raw, pos)[0]; pos += 4

    name_zstd   = raw[pos:pos + name_zstd_len]; pos += name_zstd_len
    admin_bytes = raw[pos:pos + admin_tbl_len];  pos += admin_tbl_len

    tables   = json.loads(zstd.ZstdDecompressor().decompress(name_zstd).decode())
    countries = tables["countries"]
    adm1s    = tables["adm1s"]
    adm2s    = tables["adm2s"]

    print(f"  {num_admins:,} admins, {num_polys:,} polygon parts")

    # ── Parse polygon stream, group by admin_id ──────────────────────────────
    print("Parsing polygons …")
    admin_polys = [[] for _ in range(num_admins)]

    for pi in progress(range(num_polys), total=num_polys, desc="parts"):
        admin_id  = struct.unpack_from('<H', raw, pos)[0]; pos += 2
        num_rings = struct.unpack_from('<I', raw, pos)[0]; pos += 4

        rings = []
        for _ in range(num_rings):
            np_ = struct.unpack_from('<I', raw, pos)[0]; pos += 4
            coords = struct.unpack_from(f'<{np_ * 2}f', raw, pos)
            pos += np_ * 8
            # coords is flat [lon0, lat0, lon1, lat1, …]
            rings.append(list(zip(coords[::2], coords[1::2])))

        if rings:
            try:
                poly = Polygon(rings[0], rings[1:])
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if not poly.is_empty:
                    admin_polys[admin_id].append(poly)
            except Exception:
                pass

    # ── Build features ────────────────────────────────────────────────────────
    print("Building features …")
    features = []
    skipped  = 0

    for admin_id in progress(range(num_admins), total=num_admins, desc="features"):
        parts = admin_polys[admin_id]
        if not parts:
            skipped += 1
            continue

        # Merge parts (fast for non-overlapping polygons) then simplify
        geom = unary_union(parts) if len(parts) > 1 else parts[0]
        geom = geom.simplify(SIMPLIFY_TOL, preserve_topology=True)
        if geom.is_empty:
            skipped += 1
            continue

        off   = admin_id * 5
        c_idx  = struct.unpack_from('<B', admin_bytes, off)[0]
        a1_idx = struct.unpack_from('<H', admin_bytes, off + 1)[0]
        a2_idx = struct.unpack_from('<H', admin_bytes, off + 3)[0]

        features.append({
            "type": "Feature",
            "id": admin_id,
            "properties": {
                "country": countries[c_idx],
                "adm1":    adm1s[a1_idx],
                "adm2":    adm2s[a2_idx],
            },
            "geometry": round_geom(mapping(geom)),
        })

    print(f"  {len(features):,} features, {skipped} skipped (no geometry)")

    # ── Write ─────────────────────────────────────────────────────────────────
    print(f"Writing {out_path} …")
    fc = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(out_path)
    print(f"Done: {size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
