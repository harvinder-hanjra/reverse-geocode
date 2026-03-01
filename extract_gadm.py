#!/usr/bin/env python3
"""
extract_gadm.py — Extract finest available admin level from GADM 4.1 GeoPackage.

For each GADM row, uses the deepest non-empty name level:
    NAME_5 → NAME_4 → NAME_3 → NAME_2 (fallback)

Outputs GeoJSON with properties:
    country  = GID_0
    adm1     = parent of finest level (NAME_{finest-1})
    adm2     = finest level name
    uid      = finest-level GID (globally unique, used for dedup in prepare.py)

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
    envelope_sizes = [0, 32, 48, 48, 64]
    env_size = envelope_sizes[min(envelope_type, 4)]
    wkb_offset = 8 + env_size
    return wkb.loads(blob[wkb_offset:])


def main():
    gpkg_path = sys.argv[1] if len(sys.argv) > 1 else "gadm_410.gpkg"
    out_path  = sys.argv[2] if len(sys.argv) > 2 else "gadm_finest.geojson"

    con = sqlite3.connect(gpkg_path)
    cur = con.cursor()

    cur.execute("SELECT table_name, column_name FROM gpkg_geometry_columns")
    tables = cur.fetchall()
    print("Tables in GeoPackage:")
    for t, c in tables:
        cur.execute(f"SELECT COUNT(*) FROM \"{t}\"")
        n = cur.fetchone()[0]
        print(f"  {t} ({c}) — {n:,} rows")

    table = next((t for t, _ in tables if "ADM2" in t.upper()), None)
    if not table:
        table = next((t for t, _ in tables), None)
    if not table:
        print("ERROR: no geometry table found"); sys.exit(1)
    print(f"\nUsing table: {table}")

    cur.execute(f"PRAGMA table_info(\"{table}\")")
    cols = {row[1] for row in cur.fetchall()}
    print(f"Available columns (sample): {sorted(cols)[:12]} ...")

    # Required columns
    geom_col = next((c for c in ["geom", "geometry", "GEOMETRY"] if c in cols), None)
    if not geom_col:
        print("ERROR: no geometry column found"); sys.exit(1)

    # Select all rows where at least NAME_2 is populated (includes ADM3/4/5)
    name_cols  = ["NAME_1", "NAME_2", "NAME_3", "NAME_4", "NAME_5"]
    gid_cols   = ["GID_0", "GID_2", "GID_3", "GID_4", "GID_5"]
    # Filter to only columns that exist
    have_name  = [c for c in name_cols if c in cols]
    have_gid   = [c for c in gid_cols  if c in cols]

    select_cols = ", ".join(f'"{c}"' for c in ["GID_0"] + have_name + have_gid + [geom_col])
    where = '"NAME_2" IS NOT NULL AND "NAME_2" != \'\''
    query = f'SELECT {select_cols} FROM "{table}" WHERE {where}'

    print(f"\nQuery: SELECT {', '.join(['GID_0'] + have_name + have_gid + [geom_col])}")
    print(f"WHERE {where}")
    cur.execute(query)

    # Build column index map
    col_order = ["GID_0"] + have_name + have_gid + [geom_col]
    ci = {c: i for i, c in enumerate(col_order)}

    features = []
    errors = 0
    counts = {2: 0, 3: 0, 4: 0, 5: 0}

    for i, row in enumerate(cur):
        if i % 10000 == 0:
            print(f"  {i:,} rows processed ...", end="\r", flush=True)

        geom_blob = row[ci[geom_col]]
        try:
            geom = gpkg_wkb_to_shapely(geom_blob)
        except Exception:
            errors += 1
            continue

        gid0  = row[ci["GID_0"]] or "UNK"
        n1    = row[ci["NAME_1"]] if "NAME_1" in ci else ""
        n2    = row[ci["NAME_2"]] if "NAME_2" in ci else ""
        n3    = row[ci["NAME_3"]] if "NAME_3" in ci else None
        n4    = row[ci["NAME_4"]] if "NAME_4" in ci else None
        n5    = row[ci["NAME_5"]] if "NAME_5" in ci else None

        gid2  = row[ci["GID_2"]] if "GID_2" in ci else None
        gid3  = row[ci["GID_3"]] if "GID_3" in ci else None
        gid4  = row[ci["GID_4"]] if "GID_4" in ci else None
        gid5  = row[ci["GID_5"]] if "GID_5" in ci else None

        # Pick finest available level
        if n5 and n5.strip():
            finest  = int(n5.strip())  if False else n5.strip()
            parent  = (n4 or n3 or n2 or "").strip()
            uid     = gid5 or gid4 or gid3 or gid2 or f"{gid0}_{n2}_{n5}"
            level   = 5
        elif n4 and n4.strip():
            finest  = n4.strip()
            parent  = (n3 or n2 or "").strip()
            uid     = gid4 or gid3 or gid2 or f"{gid0}_{n2}_{n4}"
            level   = 4
        elif n3 and n3.strip():
            finest  = n3.strip()
            parent  = n2.strip() if n2 else ""
            uid     = gid3 or gid2 or f"{gid0}_{n2}_{n3}"
            level   = 3
        else:
            finest  = n2.strip() if n2 else ""
            parent  = n1.strip() if n1 else ""
            uid     = gid2 or f"{gid0}_{n1}_{n2}"
            level   = 2

        counts[level] += 1

        features.append({
            "type": "Feature",
            "properties": {
                "country": gid0,
                "adm1":    parent or (n1 or "").strip(),
                "adm2":    finest,
                "uid":     uid or "",
            },
            "geometry": mapping(geom),
        })

    con.close()
    print(f"\n  Done: {len(features):,} features ({errors} geometry errors)")
    print(f"  Level breakdown: ADM2={counts[2]:,}  ADM3={counts[3]:,}  ADM4={counts[4]:,}  ADM5={counts[5]:,}")

    fc = {"type": "FeatureCollection", "features": features}
    print(f"Writing {out_path} ...")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    import os
    print(f"Done: {os.path.getsize(out_path) / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
