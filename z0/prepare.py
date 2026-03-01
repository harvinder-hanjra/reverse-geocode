#!/usr/bin/env python3
"""
prepare.py — Preprocess GeoJSON into compact binary format for builder.zig.

Usage:
    python prepare.py <input.geojson> [output.z0prep]

Output format (z0_prep.bin):
    [0:8]   "Z0PREP01"         magic
    [8:12]  num_admins         u32 LE
    [12:16] num_polys          u32 LE  (total parts after MultiPolygon flattening)
    [16:20] name_zstd_len      u32 LE
    [20:24] admin_table_len    u32 LE  (= num_admins * 6)
    [24 : 24+name_zstd_len]   zstd-compressed JSON  {"countries":[...], "adm1s":[...], "adm2s":[...]}
    [.. : ..+admin_table_len] admin table: [country_idx:u16, adm1_idx:u16, adm2_idx:u16] per entry
    [.. : ]  polygon stream, for each polygon:
               admin_id:  u32 LE
               num_rings: u32 LE
               for each ring:
                 num_pts: u32 LE
                 coords:  [num_pts * 2] f32 LE  (interleaved lon, lat)
"""

import json
import struct
import sys

import zstandard as zstd
from shapely.geometry import shape, Polygon, MultiPolygon

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

MAGIC = b"Z0PREP01"
SIMPLIFY_TOL = 0.01  # degrees (~1 km); reduces vertex count ~10x


def flatten_polygons(geom):
    """Yield simple Shapely Polygon objects from any geometry."""
    if geom is None:
        return
    if isinstance(geom, Polygon):
        if not geom.is_empty:
            p = geom if geom.is_valid else geom.buffer(0)
            if not p.is_empty:
                if isinstance(p, Polygon):
                    yield p
                else:
                    yield from flatten_polygons(p)
    elif isinstance(geom, MultiPolygon):
        for part in geom.geoms:
            yield from flatten_polygons(part)
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            yield from flatten_polygons(g)


def main():
    # Parse args: prepare.py <input.geojson> [output] [--supplement <supp.geojson>]
    import itertools
    args = sys.argv[1:]
    supp_path = None
    if "--supplement" in args:
        idx = args.index("--supplement")
        supp_path = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    geojson_path = args[0] if args else "/tmp/geoboundaries_adm2.geojson"
    out_path = args[1] if len(args) > 1 else "z0_prep.bin"

    print(f"Loading {geojson_path} …")
    # Use streaming parser for large files to avoid loading everything into RAM.
    try:
        import ijson
        def _stream_features(path):
            with open(path, "rb") as fh:
                for feat in ijson.items(fh, "features.item"):
                    yield feat
        features = _stream_features(geojson_path)
        print("  (streaming mode via ijson)")
    except ImportError:
        with open(geojson_path, encoding="utf-8") as f:
            data = json.load(f)
        features = data.get("features", [])
        print(f"  {len(features)} features")

    if supp_path:
        print(f"Appending supplement {supp_path} …")
        with open(supp_path, encoding="utf-8") as f:
            supp = json.load(f)
        features = itertools.chain(features, supp.get("features", []))

    # Admin ID tables
    country_map, adm1_map, adm2_map = {}, {}, {}
    admin_triple_map = {}
    admin_table = []
    countries_list, adm1s_list, adm2s_list = [], [], []

    def get_or_add(mapping, lst, name):
        if name not in mapping:
            mapping[name] = len(lst)
            lst.append(name)
        return mapping[name]

    # Flatten features → (admin_id, Polygon) pairs
    polys = []
    skipped = 0
    for feat in (tqdm(features, desc="Preprocessing", unit="feat") if tqdm else features):
        props = feat.get("properties") or {}
        raw_geom = feat.get("geometry")
        if not raw_geom:
            continue

        country = str(
            props.get("country") or props.get("shapeGroup") or props.get("GID_0") or
            props.get("ISO") or "UNK"
        ).strip()
        adm1 = str(
            props.get("adm1") or props.get("ADM1_NAME") or props.get("NAME_1") or ""
        ).strip()
        adm2 = str(
            props.get("adm2") or props.get("shapeName") or props.get("NAME_2") or ""
        ).strip()
        uid = str(props.get("uid") or "").strip()

        # Dedup key: include uid when present so finest-level features with the
        # same name in different regions get separate admin_ids.
        triple = (country, adm1, adm2, uid) if uid else (country, adm1, adm2)
        if triple not in admin_triple_map:
            c_idx = get_or_add(country_map, countries_list, country)
            a1_idx = get_or_add(adm1_map, adm1s_list, adm1)
            a2_idx = get_or_add(adm2_map, adm2s_list, adm2)
            admin_id = len(admin_table)
            admin_triple_map[triple] = admin_id
            admin_table.append((c_idx, a1_idx, a2_idx))

        admin_id = admin_triple_map[triple]

        try:
            geom = shape(raw_geom)
            if not geom.is_valid:
                geom = geom.buffer(0)
            # Simplify before rasterisation: primary size-reduction lever.
            geom = geom.simplify(SIMPLIFY_TOL, preserve_topology=True)
        except Exception:
            continue

        for poly in flatten_polygons(geom):
            polys.append((admin_id, poly))

    print(f"  Admin entries: {len(admin_table)}, polygon parts: {len(polys)}, skipped: {skipped}")

    # Compress name tables
    name_obj = {"countries": countries_list, "adm1s": adm1s_list, "adm2s": adm2s_list}
    name_json = json.dumps(name_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    cctx = zstd.ZstdCompressor(level=19)
    name_zstd = cctx.compress(name_json)
    print(f"  Name tables: {len(name_json):,} raw → {len(name_zstd):,} zstd")

    # Admin table bytes: 6 bytes per entry (uint16 × 3)
    admin_bytes = b"".join(struct.pack("<HHH", c, a1, a2) for c, a1, a2 in admin_table)
    assert len(admin_bytes) == len(admin_table) * 6

    print(f"Writing {out_path} …")
    with open(out_path, "wb") as f:
        # Header
        f.write(MAGIC)
        f.write(struct.pack("<IIII", len(admin_table), len(polys), len(name_zstd), len(admin_bytes)))
        # Variable sections
        f.write(name_zstd)
        f.write(admin_bytes)
        # Polygon stream (admin_id is now u32 to support >65k admins)
        for admin_id, poly in polys:
            rings = [poly.exterior] + list(poly.interiors)
            f.write(struct.pack("<II", admin_id, len(rings)))
            for ring in rings:
                coords = list(ring.coords)
                # Drop closing duplicate vertex
                if len(coords) > 1 and coords[0] == coords[-1]:
                    coords = coords[:-1]
                n = len(coords)
                f.write(struct.pack("<I", n))
                for lon, lat in coords:
                    f.write(struct.pack("<ff", float(lon), float(lat)))

    import os
    size = os.path.getsize(out_path)
    print(f"Done: {out_path} = {size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
