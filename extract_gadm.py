#!/usr/bin/env python3
"""
extract_gadm.py — Extract ADM2 layer from GADM GeoPackage into GeoJSON.

GeoPackage is a SQLite3 database. No fiona/GDAL needed.
Geometries are stored as GeoPackage-format WKB (standard WKB with a 4-byte
header prefix containing GPKG magic bytes and SRS ID — strip first 8 bytes
to get standard WKB that shapely can read).

Usage:
    python extract_gadm.py gadm_410.gpkg [output.geojson]
"""

import json
import sqlite3
import struct
import sys
from shapely import wkb
from shapely.geometry import mapping


def gpkg_wkb_to_shapely(blob: bytes):
    """Strip the 8-byte GeoPackage envelope header and parse as WKB."""
    if blob[:2] != b'GP':
        raise ValueError("Not a GeoPackage geometry blob")
    flags = blob[3]
    envelope_type = (flags >> 1) & 0x07
    # Envelope sizes: 0=no envelope, 1=bbox(32B), 2=bbox+z(48B), 3=bbox+m(48B), 4=bbox+zm(64B)
    envelope_sizes = [0, 32, 48, 48, 64]
    env_size = envelope_sizes[min(envelope_type, 4)]
    wkb_offset = 8 + env_size
    return wkb.loads(blob[wkb_offset:])


def main():
    gpkg_path = sys.argv[1] if len(sys.argv) > 1 else "gadm_410.gpkg"
    out_path  = sys.argv[2] if len(sys.argv) > 2 else "gadm_adm2.geojson"

    con = sqlite3.connect(gpkg_path)
    cur = con.cursor()

    # List geometry tables
    cur.execute("SELECT table_name, column_name FROM gpkg_geometry_columns")
    tables = cur.fetchall()
    print("Tables in GeoPackage:")
    for t, c in tables:
        cur.execute(f"SELECT COUNT(*) FROM \"{t}\"")
        n = cur.fetchone()[0]
        print(f"  {t} ({c}) — {n:,} rows")

    # Find table — GADM 4.x uses a single table for all levels
    adm2_table = next((t for t, _ in tables if "ADM2" in t.upper()), None)
    if not adm2_table:
        # GADM 4.1 single-table format
        adm2_table = next((t for t, _ in tables), None)
        if not adm2_table:
            print("ERROR: no geometry table found"); sys.exit(1)
        print(f"\nUsing single table: {adm2_table} (will filter for ADM2 level)")
    else:
        print(f"\nUsing table: {adm2_table}")

    # Get column names
    cur.execute(f"PRAGMA table_info(\"{adm2_table}\")")
    cols = [row[1] for row in cur.fetchall()]
    print(f"Columns: {cols[:10]} ...")

    # Map GADM columns to our schema
    col_map = {
        "country":  next((c for c in ["GID_0", "ISO", "ISO_3166"] if c in cols), None),
        "adm1":     next((c for c in ["NAME_1", "ADM1NAME"] if c in cols), None),
        "adm2":     next((c for c in ["NAME_2", "ADM2NAME"] if c in cols), None),
        "adm3":     next((c for c in ["NAME_3", "GID_3"] if c in cols), None),
        "geom_col": next((c for c in ["geom", "geometry", "GEOMETRY"] if c in cols), None),
    }
    print(f"Column mapping: {col_map}")

    # Select all rows with NAME_2 populated.
    # For countries with deeper hierarchies (e.g. France uses ADM3 communes),
    # GADM populates NAME_3 on every row — so filtering by NAME_3 IS NULL would
    # incorrectly drop those countries.  We keep all rows and use NAME_2 as the
    # ADM2 label; prepare.py deduplicates by (country, adm1, adm2) triple, so
    # fine-grained sub-polygons merge into the correct ADM2 admin_id.
    where = f'WHERE "{col_map["adm2"]}" IS NOT NULL AND "{col_map["adm2"]}" != \'\''

    select_cols = f'"{col_map["country"]}", "{col_map["adm1"]}", "{col_map["adm2"]}", "{col_map["geom_col"]}"'
    cur.execute(f'SELECT {select_cols} FROM "{adm2_table}" {where}')

    features = []
    errors = 0
    for i, (country, adm1, adm2, geom_blob) in enumerate(cur):
        if i % 5000 == 0:
            print(f"  {i:,} features processed ...", end="\r", flush=True)
        try:
            geom = gpkg_wkb_to_shapely(geom_blob)
        except Exception as e:
            errors += 1
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "shapeGroup": country or "UNK",
                "ADM1_NAME":  adm1 or "",
                "shapeName":  adm2 or "",
            },
            "geometry": mapping(geom),
        })

    con.close()
    print(f"\n  Done: {len(features):,} features ({errors} geometry errors)")

    fc = {"type": "FeatureCollection", "features": features}
    print(f"Writing {out_path} ...")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    import os
    print(f"Done: {os.path.getsize(out_path) / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
