#!/usr/bin/env python3
"""
builder.py — Offline reverse geocoder binary file builder.

Reads a geoBoundaries ADM2 GeoJSON file and writes z0_geo.bin according
to the RGEO0002 binary format specification.

Usage:
    python builder.py <path_to_adm2.geojson>
"""

import argparse
import json
import math
import struct
import sys
import time
from collections import defaultdict

import zstandard as zstd
from shapely.geometry import shape, Point, MultiPolygon, Polygon
from shapely.ops import unary_union


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC = b"RGEO0002"
FORMAT_VERSION = 1

GRID_COLS = 1440   # 360° / 0.25°
GRID_ROWS = 720    # 180° / 0.25°
GRID_CELL_DEG = 0.25

CELL_INTERIOR = 0xFFFF  # stored as-is but value <= 0xFFFD means interior admin_id
SENTINEL_BOUNDARY = 0xFFFF
SENTINEL_OCEAN = 0xFFFE

# Morton table block: 10 records of 6 bytes each + 4 bytes padding = 64 bytes
BLOCK_RECORDS = 10
BLOCK_SIZE = 64


# ---------------------------------------------------------------------------
# Morton code
# ---------------------------------------------------------------------------

def interleave_bits(x: int, y: int) -> int:
    """Interleave bits of two 16-bit integers into one 32-bit Morton code."""
    result = 0
    for i in range(16):
        result |= ((x >> i) & 1) << (2 * i)
        result |= ((y >> i) & 1) << (2 * i + 1)
    return result


def compute_morton(lat: float, lon: float) -> int:
    lat_q = int((lat + 90.0) / 180.0 * 65536) & 0xFFFF
    lon_q = int((lon + 180.0) / 360.0 * 65536) & 0xFFFF
    return interleave_bits(lat_q, lon_q)


def morton_to_latlon(morton: int) -> tuple:
    """Convert a Morton code back to (lat, lon) centroid of that cell."""
    lat_q = 0
    lon_q = 0
    for i in range(16):
        lat_q |= ((morton >> (2 * i)) & 1) << i
        lon_q |= ((morton >> (2 * i + 1)) & 1) << i
    # Convert quantized values back to lat/lon (centroid = cell center)
    lat = (lat_q + 0.5) / 65536.0 * 180.0 - 90.0
    lon = (lon_q + 0.5) / 65536.0 * 360.0 - 180.0
    return lat, lon


# ---------------------------------------------------------------------------
# Data ingestion
# ---------------------------------------------------------------------------

def load_geojson(path: str):
    """
    Load geoBoundaries ADM2 GeoJSON. Returns list of dicts with keys:
    country, adm1, adm2, geometry (shapely).
    """
    print(f"Loading GeoJSON from {path} ...")
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)

    features = gj.get("features", [])
    print(f"  {len(features)} features found")

    records = []
    for feat in features:
        props = feat.get("properties", {}) or {}
        geom_raw = feat.get("geometry")
        if not geom_raw:
            continue

        # geoBoundaries uses these property names (try common variants)
        country = (
            props.get("shapeGroup") or
            props.get("COUNTRY") or
            props.get("country") or
            props.get("ISO") or
            props.get("GID_0") or
            "UNK"
        )
        adm1 = (
            props.get("ADM1") or
            props.get("adm1") or
            props.get("shapeName_1") or
            props.get("NAME_1") or
            props.get("GID_1") or
            ""
        )
        adm2 = (
            props.get("shapeName") or
            props.get("ADM2") or
            props.get("adm2") or
            props.get("NAME_2") or
            props.get("GID_2") or
            props.get("name") or
            props.get("NAME") or
            ""
        )

        try:
            geom = shape(geom_raw)
            if not geom.is_valid:
                geom = geom.buffer(0)
        except Exception as e:
            print(f"  Warning: skipping feature with bad geometry: {e}")
            continue

        records.append({
            "country": str(country).strip(),
            "adm1": str(adm1).strip(),
            "adm2": str(adm2).strip(),
            "geometry": geom,
        })

    print(f"  {len(records)} valid polygon records loaded")
    return records


# ---------------------------------------------------------------------------
# Admin ID table
# ---------------------------------------------------------------------------

def build_admin_tables(records):
    """
    Deduplicate (country, adm1, adm2) triples.
    Returns:
        admin_ids: dict mapping triple -> uint16 admin_id
        countries: list of country name strings (indexed by country_idx)
        adm1s:     list of adm1 name strings
        adm2s:     list of adm2 name strings
        admin_table: list of (country_idx, adm1_idx, adm2_idx) tuples indexed by admin_id
    """
    country_map = {}
    adm1_map = {}
    adm2_map = {}
    admin_triple_map = {}
    admin_table = []

    def get_or_add(mapping, lst, name):
        if name not in mapping:
            mapping[name] = len(lst)
            lst.append(name)
        return mapping[name]

    countries_list = []
    adm1s_list = []
    adm2s_list = []

    for rec in records:
        triple = (rec["country"], rec["adm1"], rec["adm2"])
        if triple not in admin_triple_map:
            c_idx = get_or_add(country_map, countries_list, rec["country"])
            a1_idx = get_or_add(adm1_map, adm1s_list, rec["adm1"])
            a2_idx = get_or_add(adm2_map, adm2s_list, rec["adm2"])
            admin_id = len(admin_table)
            if admin_id > 0xFFFD:
                raise ValueError(f"Too many admin regions: {admin_id} > 65,533")
            admin_triple_map[triple] = admin_id
            admin_table.append((c_idx, a1_idx, a2_idx))

    print(f"  Admin IDs: {len(admin_table)}, "
          f"Countries: {len(countries_list)}, "
          f"ADM1: {len(adm1s_list)}, "
          f"ADM2: {len(adm2s_list)}")

    return admin_triple_map, countries_list, adm1s_list, adm2s_list, admin_table


# ---------------------------------------------------------------------------
# Spatial index: simple grid over polygons for fast point-in-polygon
# ---------------------------------------------------------------------------

class PolygonIndex:
    """
    A simple grid-based spatial index for point-in-polygon queries.
    Divides the world into coarse buckets and stores polygon candidates per bucket.
    """

    def __init__(self, records, admin_triple_map, bucket_deg=2.0):
        self.bucket_deg = bucket_deg
        self.cols = int(360.0 / bucket_deg)
        self.rows = int(180.0 / bucket_deg)
        self.buckets = defaultdict(list)

        print("  Building spatial index ...")
        for rec in records:
            triple = (rec["country"], rec["adm1"], rec["adm2"])
            admin_id = admin_triple_map[triple]
            geom = rec["geometry"]
            # Get bounding box and add to all overlapping buckets
            minx, miny, maxx, maxy = geom.bounds
            c0 = max(0, int((minx + 180.0) / bucket_deg))
            c1 = min(self.cols - 1, int((maxx + 180.0) / bucket_deg))
            r0 = max(0, int((miny + 90.0) / bucket_deg))
            r1 = min(self.rows - 1, int((maxy + 90.0) / bucket_deg))
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    self.buckets[(r, c)].append((admin_id, geom))

        print(f"  Spatial index built ({len(self.buckets)} non-empty buckets)")

    def query(self, lat: float, lon: float):
        """Return admin_id for (lat, lon) or None if no polygon contains it."""
        r = int((lat + 90.0) / self.bucket_deg)
        c = int((lon + 180.0) / self.bucket_deg)
        r = max(0, min(self.rows - 1, r))
        c = max(0, min(self.cols - 1, c))
        pt = Point(lon, lat)
        for admin_id, geom in self.buckets.get((r, c), []):
            if geom.contains(pt):
                return admin_id
        return None

    def query_cell_admin(self, cell_lat_center: float, cell_lon_center: float):
        """Return admin_id for center of a 0.25° cell, or None."""
        return self.query(cell_lat_center, cell_lon_center)


# ---------------------------------------------------------------------------
# Layer 0: Coarse grid
# ---------------------------------------------------------------------------

def cell_center(row: int, col: int):
    """Return (lat, lon) of the center of a coarse grid cell."""
    lon = -180.0 + (col + 0.5) * GRID_CELL_DEG
    lat = 90.0 - (row + 0.5) * GRID_CELL_DEG
    return lat, lon


def build_coarse_grid(index: PolygonIndex):
    """
    Build Layer 0 coarse grid.

    Returns:
        bitmap:      bytearray of length ceil(1036800/8), one bit per cell
        rank_table:  list of uint32 — cumulative popcount before each 512-bit block
        values:      list of uint16 — one entry per land cell (dense-packed)
        land_cells:  list of (row, col) for cells that are BOUNDARY
    """
    total_cells = GRID_ROWS * GRID_COLS  # 1,036,800

    # For each cell: determine admin_id of center (None = ocean)
    print("  Classifying coarse grid cells ...")

    # cell_admin[idx] = admin_id or None
    cell_admin = [None] * total_cells

    processed = 0
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            idx = row * GRID_COLS + col
            lat, lon = cell_center(row, col)
            cell_admin[idx] = index.query(lat, lon)
        processed += GRID_COLS
        if (row + 1) % 72 == 0:
            pct = (row + 1) / GRID_ROWS * 100
            print(f"    Grid classification: {pct:.0f}% ({row+1}/{GRID_ROWS} rows)")

    # Build bitmap, values, and determine BOUNDARY vs INTERIOR
    bitmap_bits = total_cells
    bitmap_bytes = (bitmap_bits + 7) // 8
    # Pad to 64-byte alignment
    bitmap_bytes_aligned = ((bitmap_bytes + 63) // 64) * 64
    bitmap = bytearray(bitmap_bytes_aligned)

    values = []
    boundary_cells = []

    print("  Building bitmap and classifying INTERIOR vs BOUNDARY ...")

    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            idx = row * GRID_COLS + col
            admin_id = cell_admin[idx]

            if admin_id is None:
                # Ocean cell — bit stays 0
                continue

            # Set land bit
            byte_idx = idx // 8
            bit_idx = idx % 8
            bitmap[byte_idx] |= (1 << bit_idx)

            # Check all 8 neighbors for INTERIOR classification
            all_same = True
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr = row + dr
                    nc = (col + dc) % GRID_COLS  # wrap longitude
                    if nr < 0 or nr >= GRID_ROWS:
                        # Pole neighbor — treat as same admin if current is land
                        continue
                    nidx = nr * GRID_COLS + nc
                    if cell_admin[nidx] != admin_id:
                        all_same = False
                        break
                if not all_same:
                    break

            if all_same:
                # INTERIOR: store admin_id directly
                values.append(admin_id)
            else:
                # BOUNDARY: store sentinel
                values.append(SENTINEL_BOUNDARY)
                boundary_cells.append((row, col))

    # Build rank table: one uint32 per 512-bit block
    # rank_table[i] = number of set bits in bitmap before bit position i*512
    num_blocks = (bitmap_bits + 511) // 512
    rank_table = []
    cumulative = 0
    for block_i in range(num_blocks):
        rank_table.append(cumulative)
        # Count bits in this 512-bit (64-byte) block
        start_byte = block_i * 64
        end_byte = min(start_byte + 64, bitmap_bytes_aligned)
        for b in range(start_byte, end_byte):
            cumulative += bin(bitmap[b]).count('1')

    land_count = sum(1 for v in values if v is not None)
    boundary_count = sum(1 for v in values if v == SENTINEL_BOUNDARY)
    interior_count = land_count - boundary_count

    print(f"  Land cells: {land_count}, Interior: {interior_count}, "
          f"Boundary: {boundary_count}")

    return bitmap, rank_table, values, boundary_cells


# ---------------------------------------------------------------------------
# Layer 1: Morton boundary table
# ---------------------------------------------------------------------------

def coarse_cell_morton_range(row: int, col: int):
    """
    Return the range of Morton codes covering a 0.25° coarse cell.

    A 0.25° cell in lat/lon maps to a range of quantized coordinates.
    lat range: [90 - (row+1)*0.25, 90 - row*0.25)
    lon range: [-180 + col*0.25, -180 + (col+1)*0.25)
    """
    lat_hi = 90.0 - row * GRID_CELL_DEG
    lat_lo = 90.0 - (row + 1) * GRID_CELL_DEG
    lon_lo = -180.0 + col * GRID_CELL_DEG
    lon_hi = -180.0 + (col + 1) * GRID_CELL_DEG

    # Quantize to 16-bit
    lat_q_lo = max(0, int((lat_lo + 90.0) / 180.0 * 65536))
    lat_q_hi = min(65535, int((lat_hi + 90.0) / 180.0 * 65536))
    lon_q_lo = max(0, int((lon_lo + 180.0) / 360.0 * 65536))
    lon_q_hi = min(65535, int((lon_hi + 180.0) / 360.0 * 65536))

    return lat_q_lo, lat_q_hi, lon_q_lo, lon_q_hi


def build_morton_table(boundary_cells, index: PolygonIndex):
    """
    For each BOUNDARY coarse cell, enumerate all Morton codes within it,
    do point-in-polygon for each centroid, and return sorted list of
    (morton, admin_id) records.

    Builds a single STRtree over all polygons once, then issues one
    vectorized query per boundary coarse cell.
    """
    from shapely import points as shapely_points
    from shapely.strtree import STRtree
    import numpy as np
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    total = len(boundary_cells)
    print(f"  Building Morton table for {total} boundary cells ...")

    # Deduplicate polygons: one geometry per admin_id, built into STRtree once
    seen: dict[int, object] = {}
    for bucket in index.buckets.values():
        for admin_id, geom in bucket:
            if admin_id not in seen:
                seen[admin_id] = geom
    admin_ids_list = sorted(seen.keys())
    admin_geoms = [seen[aid] for aid in admin_ids_list]
    tree = STRtree(admin_geoms)
    admin_ids_arr = np.array(admin_ids_list, dtype=np.int32)

    records = []
    iterator = tqdm(boundary_cells, desc="  Morton rasterize", unit="cell") if tqdm else boundary_cells

    for row, col in iterator:
        lat_q_lo, lat_q_hi, lon_q_lo, lon_q_hi = coarse_cell_morton_range(row, col)

        # All (lat_q, lon_q) pairs within this coarse cell via numpy meshgrid
        lat_qs = np.arange(lat_q_lo, lat_q_hi + 1, dtype=np.int32)
        lon_qs = np.arange(lon_q_lo, lon_q_hi + 1, dtype=np.int32)
        latq_grid, lonq_grid = np.meshgrid(lat_qs, lon_qs, indexing='ij')
        latq_flat = latq_grid.ravel()
        lonq_flat = lonq_grid.ravel()

        lat_flat = (latq_flat.astype(np.float64) + 0.5) / 65536.0 * 180.0 - 90.0
        lon_flat = (lonq_flat.astype(np.float64) + 0.5) / 65536.0 * 360.0 - 180.0

        # Single vectorized STRtree query over all ~4K points in this cell.
        # shapely 2.0: query(array, predicate) -> (input_indices, tree_indices)
        pts = shapely_points(lon_flat, lat_flat)
        input_idx, tree_idx = tree.query(pts, predicate='within')

        for p_i, g_i in zip(input_idx, tree_idx):
            morton = interleave_bits(int(latq_flat[p_i]), int(lonq_flat[p_i]))
            records.append((morton, int(admin_ids_arr[g_i])))

        if tqdm is None and (len(records) % 100000 == 0) and records:
            print(f"    {len(records)} Morton records so far ...")

    print(f"  Sorting {len(records)} Morton records ...")
    records.sort(key=lambda r: r[0])

    # Deduplicate: same morton code → keep first occurrence
    deduped: list[tuple[int, int]] = []
    prev_morton = -1
    for morton, admin_id in records:
        if morton != prev_morton:
            deduped.append((morton, admin_id))
            prev_morton = morton

    print(f"  After dedup: {len(deduped)} Morton records")
    return deduped


def pack_morton_blocks(records):
    """
    Pack Morton records into 64-byte blocks (10 records × 6 bytes + 4 pad).
    Returns (block_data: bytes, directory: list of uint32).
    """
    block_data = bytearray()
    directory = []

    for i in range(0, len(records), BLOCK_RECORDS):
        chunk = records[i:i + BLOCK_RECORDS]
        directory.append(chunk[0][0])  # first morton of block

        block = bytearray(BLOCK_SIZE)
        for j, (morton, admin_id) in enumerate(chunk):
            offset = j * 6
            struct.pack_into("<IH", block, offset, morton, admin_id)
        # Last 4 bytes are padding (already zero)
        block_data.extend(block)

    return bytes(block_data), directory


# ---------------------------------------------------------------------------
# Name table serialization (zstd-compressed JSON)
# ---------------------------------------------------------------------------

def compress_name_tables(countries, adm1s, adm2s):
    """
    Serialize name lists as JSON arrays and zstd-compress them together.
    Returns compressed bytes.
    """
    payload = json.dumps({
        "countries": countries,
        "adm1s": adm1s,
        "adm2s": adm2s,
    }, ensure_ascii=False, separators=(",", ":"))
    data = payload.encode("utf-8")
    cctx = zstd.ZstdCompressor(level=19)
    compressed = cctx.compress(data)
    print(f"  Name tables: {len(data)} bytes raw -> {len(compressed)} bytes compressed")
    return compressed


# ---------------------------------------------------------------------------
# Binary file writer
# ---------------------------------------------------------------------------

def write_binary_file(
    out_path: str,
    bitmap: bytearray,
    rank_table: list,
    values: list,
    block_data: bytes,
    directory: list,
    morton_record_count: int,
    admin_table: list,
    name_table_bytes: bytes,
):
    print(f"  Writing {out_path} ...")

    # --- Serialize sections ---

    # Bitmap (already padded to 64-byte alignment)
    bitmap_bytes = bytes(bitmap)

    # Rank table: 2025 × uint32
    rank_bytes = struct.pack(f"<{len(rank_table)}I", *rank_table)

    # Values: list of uint16
    values_bytes = struct.pack(f"<{len(values)}H", *values)

    # Admin table: each entry is uint8 + uint16 + uint16 = 5 bytes
    admin_parts = []
    for c_idx, a1_idx, a2_idx in admin_table:
        admin_parts.append(struct.pack("<BHH", c_idx, a1_idx, a2_idx))
    admin_bytes = b"".join(admin_parts)

    # Morton block array and directory
    dir_bytes = struct.pack(f"<{len(directory)}I", *directory)

    # --- Compute offsets ---
    HEADER_SIZE = 64
    bitmap_offset = HEADER_SIZE
    rank_offset = bitmap_offset + len(bitmap_bytes)
    values_offset = rank_offset + len(rank_bytes)
    morton_block_offset = values_offset + len(values_bytes)
    morton_dir_offset = morton_block_offset + len(block_data)
    admin_offset = morton_dir_offset + len(dir_bytes)
    name_offset = admin_offset + len(admin_bytes)

    total_size = name_offset + len(name_table_bytes)

    print(f"  Section sizes:")
    print(f"    Header:        {HEADER_SIZE:>10,} bytes")
    print(f"    Bitmap:        {len(bitmap_bytes):>10,} bytes")
    print(f"    Rank table:    {len(rank_bytes):>10,} bytes")
    print(f"    Values:        {len(values_bytes):>10,} bytes")
    print(f"    Morton blocks: {len(block_data):>10,} bytes")
    print(f"    Directory:     {len(dir_bytes):>10,} bytes")
    print(f"    Admin table:   {len(admin_bytes):>10,} bytes")
    print(f"    Name tables:   {len(name_table_bytes):>10,} bytes (compressed)")
    print(f"    TOTAL:         {total_size:>10,} bytes ({total_size/1024/1024:.1f} MB)")

    # --- Write header ---
    timestamp = int(time.time())
    header = struct.pack(
        "<8sIIIIIIIIIIIIQ",
        MAGIC,
        FORMAT_VERSION,
        timestamp,
        bitmap_offset,
        rank_offset,
        values_offset,
        len(values),             # grid land cell count
        morton_block_offset,
        morton_dir_offset,
        morton_record_count,     # Morton record count
        len(directory),          # Morton block count
        admin_offset,
        name_offset,
        0,                       # reserved (8 bytes = uint64)
    )
    assert len(header) == HEADER_SIZE, f"Header size mismatch: {len(header)}"

    with open(out_path, "wb") as f:
        f.write(header)
        f.write(bitmap_bytes)
        f.write(rank_bytes)
        f.write(values_bytes)
        f.write(block_data)
        f.write(dir_bytes)
        f.write(admin_bytes)
        f.write(name_table_bytes)

    print(f"  Done: {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build z0_geo.bin offline reverse geocoder data file"
    )
    parser.add_argument("geojson", help="Path to ADM2 GeoJSON file")
    parser.add_argument(
        "-o", "--output", default="z0_geo.bin",
        help="Output binary file (default: z0_geo.bin)"
    )
    args = parser.parse_args()

    t0 = time.time()

    # Step 1: Load polygons
    records = load_geojson(args.geojson)
    if not records:
        print("ERROR: No valid polygon records found.", file=sys.stderr)
        sys.exit(1)

    # Step 2: Build admin tables
    print("\nStep 2: Building admin ID tables ...")
    admin_triple_map, countries, adm1s, adm2s, admin_table = build_admin_tables(records)

    # Step 3: Build spatial index
    print("\nStep 3: Building spatial index ...")
    index = PolygonIndex(records, admin_triple_map)

    # Step 4: Build coarse grid (Layer 0)
    print("\nStep 4: Building coarse grid (Layer 0) ...")
    bitmap, rank_table, values, boundary_cells = build_coarse_grid(index)

    # Step 5: Build Morton boundary table (Layer 1)
    print("\nStep 5: Building Morton boundary table (Layer 1) ...")
    morton_records = build_morton_table(boundary_cells, index)
    block_data, directory = pack_morton_blocks(morton_records)

    # Step 6: Compress name tables
    print("\nStep 6: Compressing name tables ...")
    name_table_bytes = compress_name_tables(countries, adm1s, adm2s)

    # Step 7: Write binary file
    print(f"\nStep 7: Writing binary file ...")
    write_binary_file(
        args.output,
        bitmap,
        rank_table,
        values,
        block_data,
        directory,
        len(morton_records),
        admin_table,
        name_table_bytes,
    )

    elapsed = time.time() - t0
    print(f"\nBuild complete in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
