"""
builder.py — Offline Reverse Geocoder Binary File Builder

Reads a GeoJSON FeatureCollection of ADM2 polygons and writes the binary
file s2_geo.bin as specified in spec.md.

S2 note: The native s2geometry Python bindings are not available in this
environment. This implementation uses H3 (pip install h3) as a spatial
index substitute:

  H3 resolution 6  (edge ~3.7 km, 14.1M cells)  ≈ S2 level 10 (edge ~6 km)
  H3 resolution 7  (edge ~1.4 km, 98.8M cells)   ≈ S2 level 12 (edge ~1.5 km)

Cell ID encoding (uint32):
  res6:  strip constant H3 header (bits 63-52) and 9 filler digit-slots (bits 26-0)
         result is bits 51-27 = base_cell(7) + 6 digits(18) = 25 bits
         encode = (h3_int >> 27) & 0x1FFFFFF

  res7:  strip constant H3 header (bits 63-52) and 8 filler digit-slots (bits 23-0)
         result is bits 51-24 = base_cell(7) + 7 digits(21) = 28 bits
         encode = (h3_int >> 24) & 0xFFFFFFF

  NOTE: H3 uses an aperture-7 (not quad-tree) hierarchy.  The parent of a
  res-7 cell at res-6 is NOT obtained by enc7 >> 3.  Use h3.cell_to_parent()
  and encode the result independently.

File layout (all integers little-endian, 64-byte aligned):
  [0:8]   Magic "RGEO0001"
  [8:12]  Version uint32 = 1
  [12:16] L10 (res6) record count uint32
  [16:20] L12 (res7) record count uint32
  [20:24] L10 directory offset uint32
  [24:28] L12 directory offset uint32
  [28:32] Admin table offset uint32
  [32:36] Name table offset uint32
  [36:64] reserved padding
  [64:]   L10 block array  (64-byte blocks, ≤10 × 6-byte records + 4 pad)
          L10 directory    (uint32 array, first cell_id of each block)
          L12 block array
          L12 directory
          Admin lookup table  (N × 5 bytes: uint8 country_idx, uint16 adm1_idx, uint16 adm2_idx)
          Name table          (zstd-compressed JSON)

Usage:
  python builder.py <geojson_path> [--output s2_geo.bin] [--workers N]
"""

import argparse
import json
import logging
import os
import struct
import sys
from typing import Dict, List, Optional, Tuple

import h3
import zstandard as zstd
from shapely.geometry import (
    MultiPolygon,
    Point,
    Polygon,
    mapping,
    shape,
)
from shapely.ops import unary_union
from shapely.validation import make_valid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

MAGIC = b"RGEO0001"
VERSION = 1

H3_RES_COARSE = 6   # ~3.7 km edge; analogous to S2 level 10
H3_RES_FINE   = 7   # ~1.4 km edge; analogous to S2 level 12

RECORDS_PER_BLOCK = 10
RECORD_SIZE       = 6   # uint32 cell_id + uint16 admin_id
BLOCK_SIZE        = 64  # bytes (one cache line)

HEADER_SIZE = 64  # bytes (padded to cache-line alignment)

# ── H3 cell-ID encoding ─────────────────────────────────────────────────────

def encode_res6(h3_int: int) -> int:
    """
    Compact uint32 encoding for H3 resolution-6 cell.

    H3 index layout:
      Bits 63-60: high nibble — always 0x8 (mode=1 in bits 62-59, bit63=0)
      Bits 59-56: mode-dep + reserved — always 0x0
      Bits 55-52: resolution — always 0x6 for res-6 cells
      Bits 51-45: base cell (7 bits)
      Bits 44-27: 6 digit-slots × 3 bits = 18 bits of valid path
      Bits 26-0:  9 filler digit-slots × 3 bits = 27 bits, all set to 0x7 (binary 111)

    We strip the constant 12-bit header (bits 63-52) and the 27 filler bits,
    keeping bits 51-27 = 25 bits of unique cell-path data.
    """
    return (h3_int >> 27) & 0x1FFFFFF  # 25 bits


def encode_res7(h3_int: int) -> int:
    """
    Compact uint32 encoding for H3 resolution-7 cell.

    Same as encode_res6, but resolution is 7, so:
      Bits 44-24: 7 digit-slots × 3 bits = 21 bits of valid path
      Bits 23-0:  8 filler digit-slots × 3 bits = 24 bits

    Keeping bits 51-24 = 28 bits of unique cell-path data.
    """
    return (h3_int >> 24) & 0xFFFFFFF  # 28 bits


def h3str_to_encoded(cell_str: str, res: int) -> int:
    """Encode an H3 cell string to compact uint32."""
    h3_int = h3.str_to_int(cell_str)
    if res == H3_RES_COARSE:
        return encode_res6(h3_int)
    return encode_res7(h3_int)


# ── Admin ID registry ───────────────────────────────────────────────────────

class AdminRegistry:
    """Deduplicates (country, adm1, adm2) triples and assigns uint16 IDs."""

    def __init__(self) -> None:
        self._triple_to_id: Dict[Tuple[str, str, str], int] = {}
        self._countries:  List[str] = []
        self._adm1s:      List[str] = []
        self._adm2s:      List[str] = []
        self._country_idx: Dict[str, int] = {}
        self._adm1_idx:    Dict[str, int] = {}
        self._adm2_idx:    Dict[str, int] = {}
        self._triples:    List[Tuple[int, int, int]] = []

    def _intern(self, store: List[str], index: Dict[str, int], name: str) -> int:
        if name not in index:
            index[name] = len(store)
            store.append(name)
        return index[name]

    def get_or_create(self, country: str, adm1: str, adm2: str) -> int:
        key = (country, adm1, adm2)
        if key in self._triple_to_id:
            return self._triple_to_id[key]
        admin_id = len(self._triple_to_id)
        if admin_id > 65535:
            raise ValueError(f"Admin ID overflow at {admin_id}: {key}")
        self._triple_to_id[key] = admin_id
        c_idx  = self._intern(self._countries, self._country_idx, country)
        a1_idx = self._intern(self._adm1s,     self._adm1_idx,    adm1)
        a2_idx = self._intern(self._adm2s,     self._adm2_idx,    adm2)
        self._triples.append((c_idx, a1_idx, a2_idx))
        return admin_id

    @property
    def count(self) -> int:
        return len(self._triples)

    def admin_table_bytes(self) -> bytes:
        """Serialise as count × 5 bytes (uint8 LE, uint16 LE, uint16 LE)."""
        buf = bytearray()
        for c_idx, a1_idx, a2_idx in self._triples:
            buf += struct.pack("<BHH", c_idx, a1_idx, a2_idx)
        return bytes(buf)

    def name_table_json(self) -> bytes:
        """Return UTF-8 JSON of all name lists (to be zstd-compressed)."""
        data = {
            "countries": self._countries,
            "adm1":      self._adm1s,
            "adm2":      self._adm2s,
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _to_multipolygon(geom) -> Optional[MultiPolygon]:
    """Normalise any Shapely geometry to a MultiPolygon, or None if empty."""
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    # GeometryCollection or other: keep only polygonal parts
    polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
    if not polys:
        return None
    merged = unary_union(polys)
    if merged.geom_type == "Polygon":
        return MultiPolygon([merged])
    return merged


def _geom_to_h3shape(mp: MultiPolygon):
    """Convert Shapely MultiPolygon to H3 LatLngPoly or LatLngMultiPoly."""
    parts = []
    for poly in mp.geoms:
        exterior = [(lat, lng) for lng, lat in poly.exterior.coords]
        holes    = [[(lat, lng) for lng, lat in ring.coords]
                    for ring in poly.interiors]
        parts.append(h3.LatLngPoly(exterior, *holes))
    if len(parts) == 1:
        return parts[0]
    return h3.LatLngMultiPoly(*parts)


def _cell_polygon(cell_str: str) -> Polygon:
    """Return a Shapely Polygon for an H3 cell boundary."""
    boundary = h3.cell_to_boundary(cell_str)  # list of (lat, lng)
    return Polygon([(lng, lat) for lat, lng in boundary])


# ── Per-polygon processing ───────────────────────────────────────────────────

def process_polygon(
    geom_dict: dict,
    country: str,
    adm1: str,
    adm2: str,
) -> Tuple[List[Tuple[int, str, str, str]], List[Tuple[int, str, str, str]]]:
    """
    Classify H3 cells for a single ADM2 polygon.

    Algorithm (mirrors spec §6 build pipeline):

    Coarse table (res-6 / L10) — interior cells:
      1. Compute H3 res-6 covering via h3shape_to_cells.
      2. For each covered res-6 cell whose full polygon is geometrically
         contained within the ADM2 polygon → emit to coarse table.

    Fine table (res-7 / L12) — boundary cells:
      H3's h3shape_to_cells uses centroid-in-polygon for inclusion, so some
      res-6 cells near the polygon edge are excluded even though they
      physically intersect the polygon.  Their res-7 children would then be
      silently missed if we relied only on refinement of covered res-6 cells.

      To guarantee complete coverage, we compute the res-7 covering directly
      via h3shape_to_cells at res-7.  We then exclude any res-7 cell that is
      a child of a res-6 INTERIOR cell (those are already in the coarse table
      and do not need fine-level entries).  The remaining res-7 cells are
      emitted to the fine table.

    Returns:
      coarse_records: list of (encoded_res6, country, adm1, adm2)
      fine_records:   list of (encoded_res7, country, adm1, adm2)
    """
    geom = shape(geom_dict)
    mp   = _to_multipolygon(geom)
    if mp is None:
        return [], []

    h3shape = _geom_to_h3shape(mp)

    # ── Step 1: Compute res-6 covering ──────────────────────────────────────
    try:
        cells_res6 = set(h3.h3shape_to_cells(h3shape, H3_RES_COARSE))
    except Exception as exc:
        log.warning(
            "h3shape_to_cells res6 failed for %s/%s/%s: %s", country, adm1, adm2, exc
        )
        return [], []

    coarse_records: List[Tuple[int, str, str, str]] = []
    interior_cells6: set = set()  # res-6 cells classified as interior

    # ── Step 2: Classify each res-6 cell ────────────────────────────────────
    for cell_str in cells_res6:
        cell_poly = _cell_polygon(cell_str)
        if mp.contains(cell_poly):
            # Interior: entire cell lies within the polygon
            enc = h3str_to_encoded(cell_str, H3_RES_COARSE)
            coarse_records.append((enc, country, adm1, adm2))
            interior_cells6.add(cell_str)
        # Boundary cells (straddle polygon edge) are handled at res-7 below.

    # ── Step 3: Compute res-7 covering for boundary regions ─────────────────
    # We directly cover the polygon at res-7 to handle all boundary cases,
    # including res-7 cells whose res-6 parent was not returned by
    # h3shape_to_cells (because h3shape_to_cells uses centroid-in-polygon
    # and can exclude res-6 cells whose centroid is outside the polygon but
    # which still overlap it physically).
    try:
        cells_res7 = set(h3.h3shape_to_cells(h3shape, H3_RES_FINE))
    except Exception as exc:
        log.warning(
            "h3shape_to_cells res7 failed for %s/%s/%s: %s", country, adm1, adm2, exc
        )
        return coarse_records, []

    fine_records: List[Tuple[int, str, str, str]] = []
    for cell7_str in cells_res7:
        parent6 = h3.cell_to_parent(cell7_str, H3_RES_COARSE)
        if parent6 in interior_cells6:
            # Parent is a fully-interior res-6 cell; the coarse table covers this
            # point already and no fine entry is needed.
            continue
        # Boundary res-7 cell: include in fine table.
        enc = h3str_to_encoded(cell7_str, H3_RES_FINE)
        fine_records.append((enc, country, adm1, adm2))

    return coarse_records, fine_records


# ── Block packing ────────────────────────────────────────────────────────────

def pack_into_blocks(
    records: List[Tuple[int, int]],
) -> Tuple[bytes, bytes]:
    """
    Sort records by cell_id, pack into 64-byte blocks, build directory.

    Each block holds up to 10 records (10 × 6 = 60 bytes) + 4 bytes padding.
    The directory stores the first cell_id of each block as a uint32 array.

    Args:
      records: list of (cell_id_encoded: uint32, admin_id: uint16)

    Returns:
      (block_bytes, directory_bytes)
    """
    records.sort(key=lambda r: r[0])

    blocks_buf = bytearray()
    dir_buf    = bytearray()

    i = 0
    n = len(records)
    while i < n:
        chunk = records[i:i + RECORDS_PER_BLOCK]
        i += RECORDS_PER_BLOCK

        first_cell = chunk[0][0]
        dir_buf += struct.pack("<I", first_cell)

        block = bytearray()
        for cell_id, admin_id in chunk:
            block += struct.pack("<IH", cell_id, admin_id)
        # Pad to 64 bytes (4 bytes of zero padding after last record)
        block += b"\x00" * (BLOCK_SIZE - len(block))
        assert len(block) == BLOCK_SIZE
        blocks_buf += block

    return bytes(blocks_buf), bytes(dir_buf)


# ── GeoJSON property key probing ─────────────────────────────────────────────

COUNTRY_KEYS = [
    "shapeGroup", "country", "COUNTRY", "ADM0_NAME", "admin0Name",
    "NAME_0", "name_0", "ISO_A2", "GID_0", "ADMIN",
]
ADM1_KEYS = [
    "ADM1_NAME", "admin1Name", "NAME_1", "name_1",
    "GID_1", "ADM1", "shapeName",
]
ADM2_KEYS = [
    "ADM2_NAME", "admin2Name", "NAME_2", "name_2",
    "GID_2", "ADM2", "shapeName",
]


def _pick(props: dict, keys: List[str], fallback: str = "Unknown") -> str:
    """Return the first non-empty value from props matching any key."""
    for k in keys:
        v = props.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return fallback


def load_geojson_features(path: str) -> List[dict]:
    """Load a GeoJSON file, return list of Feature dicts."""
    log.info("Loading GeoJSON from %s", path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") == "FeatureCollection":
        features = data["features"]
    elif data.get("type") == "Feature":
        features = [data]
    else:
        raise ValueError(f"Unexpected GeoJSON type: {data.get('type')!r}")

    log.info("Loaded %d features", len(features))
    return features


# ── Main build pipeline ──────────────────────────────────────────────────────

def build(geojson_path: str, output_path: str) -> None:
    """
    Full build pipeline:
      1. Ingest source polygons.
      2. Assign admin IDs (deduplicate triples).
      3. Compute H3 res-6 covering (INTERIOR / BOUNDARY classification).
      4. Refine boundary cells at res-7 using centroid containment.
      5. Sort, pack into 64-byte blocks, build directory arrays.
      6. Write binary file.
    """
    features = load_geojson_features(geojson_path)
    registry = AdminRegistry()

    # Cell → admin_id maps (last polygon wins on overlap)
    seen_coarse: Dict[int, int] = {}
    seen_fine:   Dict[int, int] = {}

    total = len(features)
    log.info("Processing %d features...", total)

    try:
        from tqdm import tqdm as _tqdm
        _iter = _tqdm(enumerate(features, 1), total=total, desc="  S2/H3 cells", unit="feat")
    except ImportError:
        _iter = enumerate(features, 1)
    for done, feat in _iter:
        props   = feat.get("properties") or {}
        country = _pick(props, COUNTRY_KEYS)
        adm1    = _pick(props, ADM1_KEYS)
        adm2    = _pick(props, ADM2_KEYS)
        geom_d  = feat.get("geometry")
        if geom_d is None:
            continue

        try:
            cr, fr = process_polygon(geom_d, country, adm1, adm2)
        except Exception as exc:
            log.warning("Skipping %s/%s/%s: %s", country, adm1, adm2, exc)
            continue

        admin_id = registry.get_or_create(country, adm1, adm2)

        for enc, *_ in cr:
            seen_coarse[enc] = admin_id
        for enc, *_ in fr:
            seen_fine[enc] = admin_id

        if done % 1000 == 0 or done == total:
            log.info(
                "  %d/%d features  (coarse=%d  fine=%d)",
                done, total, len(seen_coarse), len(seen_fine),
            )

    log.info("Admin registry: %d unique triples", registry.count)
    log.info("Coarse (res6) cells: %d", len(seen_coarse))
    log.info("Fine   (res7) cells: %d", len(seen_fine))

    # ── Pack into blocks ─────────────────────────────────────────────────────
    log.info("Packing coarse (L10) blocks...")
    l10_blocks, l10_dir = pack_into_blocks(list(seen_coarse.items()))
    log.info("Packing fine   (L12) blocks...")
    l12_blocks, l12_dir = pack_into_blocks(list(seen_fine.items()))

    l10_count = len(seen_coarse)
    l12_count = len(seen_fine)
    l10_block_cnt = len(l10_dir) // 4
    l12_block_cnt = len(l12_dir) // 4
    log.info("L10: %d records in %d blocks", l10_count, l10_block_cnt)
    log.info("L12: %d records in %d blocks", l12_count, l12_block_cnt)

    # ── Compress name table ──────────────────────────────────────────────────
    log.info("Compressing name table (zstd)...")
    name_json       = registry.name_table_json()
    cctx            = zstd.ZstdCompressor(level=19)
    name_compressed = cctx.compress(name_json)
    log.info(
        "Name table: %d bytes raw → %d bytes compressed",
        len(name_json), len(name_compressed),
    )

    admin_bytes = registry.admin_table_bytes()
    log.info(
        "Admin table: %d entries × 5 bytes = %d bytes",
        registry.count, len(admin_bytes),
    )

    # ── Compute section offsets ──────────────────────────────────────────────
    l10_blocks_offset = HEADER_SIZE
    l10_dir_offset    = l10_blocks_offset + len(l10_blocks)
    l12_blocks_offset = l10_dir_offset    + len(l10_dir)
    l12_dir_offset    = l12_blocks_offset + len(l12_blocks)
    admin_offset      = l12_dir_offset    + len(l12_dir)
    name_offset       = admin_offset      + len(admin_bytes)
    total_size        = name_offset       + len(name_compressed)

    # ── Assemble header ──────────────────────────────────────────────────────
    header = bytearray(HEADER_SIZE)
    header[0:8] = MAGIC
    struct.pack_into("<I", header, 8,  VERSION)
    struct.pack_into("<I", header, 12, l10_count)
    struct.pack_into("<I", header, 16, l12_count)
    struct.pack_into("<I", header, 20, l10_dir_offset)
    struct.pack_into("<I", header, 24, l12_dir_offset)
    struct.pack_into("<I", header, 28, admin_offset)
    struct.pack_into("<I", header, 32, name_offset)
    # Bytes 36–63: reserved, already zero.

    # ── Write output file ────────────────────────────────────────────────────
    log.info("Writing %s  (%.2f MB)...", output_path, total_size / 1e6)
    with open(output_path, "wb") as f:
        f.write(header)
        f.write(l10_blocks)
        f.write(l10_dir)
        f.write(l12_blocks)
        f.write(l12_dir)
        f.write(admin_bytes)
        f.write(name_compressed)

    actual_size = os.path.getsize(output_path)
    log.info(
        "Done. Output: %s  (%.2f MB, %d bytes)",
        output_path, actual_size / 1e6, actual_size,
    )

    _verify_file(output_path)


def _verify_file(path: str) -> None:
    """Quick structural self-check on the written file."""
    with open(path, "rb") as f:
        raw = f.read(HEADER_SIZE)

    magic       = raw[0:8]
    version,    = struct.unpack_from("<I", raw, 8)
    l10_count,  = struct.unpack_from("<I", raw, 12)
    l12_count,  = struct.unpack_from("<I", raw, 16)
    l10_dir,    = struct.unpack_from("<I", raw, 20)
    l12_dir,    = struct.unpack_from("<I", raw, 24)
    admin_off,  = struct.unpack_from("<I", raw, 28)
    name_off,   = struct.unpack_from("<I", raw, 32)

    assert magic == MAGIC,   f"Bad magic: {magic!r}"
    assert version == 1,     f"Unexpected version: {version}"
    assert l10_dir % 4 == 0, "L10 directory not 4-byte aligned"
    assert l12_dir % 4 == 0, "L12 directory not 4-byte aligned"
    # Admin entries are 5 bytes each; no power-of-two alignment constraint.

    file_size = os.path.getsize(path)
    assert name_off < file_size, "Name table offset beyond EOF"

    log.info(
        "Verification OK: L10=%d recs, L12=%d recs | "
        "l10_dir@%d  l12_dir@%d  admin@%d  names@%d",
        l10_count, l12_count, l10_dir, l12_dir, admin_off, name_off,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build s2_geo.bin reverse-geocoder index from GeoJSON.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "geojson",
        help="Path to GeoJSON FeatureCollection of ADM2 polygons.",
    )
    parser.add_argument(
        "--output", "-o",
        default="s2_geo.bin",
        help="Output binary file path.",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.geojson):
        print(f"Error: file not found: {args.geojson}", file=sys.stderr)
        sys.exit(1)

    build(args.geojson, args.output)


if __name__ == "__main__":
    main()
