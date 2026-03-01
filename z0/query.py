#!/usr/bin/env python3
"""
query.py — Offline reverse geocoder query engine.

Loads z0_geo.bin and provides reverse geocode lookups.

Usage:
    python query.py <lat> <lon>
    python query.py 48.8566 2.3522    # Paris, France
"""

import bisect
import json
import mmap
import os
import struct
import sys
from typing import Optional

import zstandard as zstd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC = b"RGEO0002"
HEADER_SIZE = 64

GRID_COLS = 1440
GRID_ROWS = 720
GRID_CELL_DEG = 0.25

SENTINEL_BOUNDARY = 0xFFFF
SENTINEL_OCEAN = 0xFFFE

BLOCK_RECORDS = 10
BLOCK_SIZE = 64
RECORD_SIZE = 6   # uint32 morton + uint16 admin_id


# ---------------------------------------------------------------------------
# Morton code (query side)
# ---------------------------------------------------------------------------

def interleave_bits(x: int, y: int) -> int:
    """Interleave bits of two 16-bit integers into a 32-bit Morton code."""
    result = 0
    for i in range(16):
        result |= ((x >> i) & 1) << (2 * i)
        result |= ((y >> i) & 1) << (2 * i + 1)
    return result


MORTON_STEPS = 4096  # 12-bit quantization; must match builder

def compute_morton(lat: float, lon: float) -> int:
    lat_q = int((lat + 90.0) / 180.0 * MORTON_STEPS) & (MORTON_STEPS - 1)
    lon_q = int((lon + 180.0) / 360.0 * MORTON_STEPS) & (MORTON_STEPS - 1)
    return interleave_bits(lat_q, lon_q)


# ---------------------------------------------------------------------------
# ReverseGeocoder
# ---------------------------------------------------------------------------

class ReverseGeocoder:
    """
    Offline reverse geocoder backed by a memory-mapped z0_geo.bin file.

    Thread-safe for concurrent reads (mmap is read-only after __init__).
    """

    def __init__(self, data_path: str = "z0_geo.bin"):
        self._f = open(data_path, "rb")
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        self._parse_header()
        self._load_name_tables()

    def close(self):
        self._mm.close()
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Header parsing
    # ------------------------------------------------------------------

    def _parse_header(self):
        mm = self._mm

        # Verify magic
        magic = mm[0:8]
        if magic != MAGIC:
            raise ValueError(f"Bad magic: {magic!r}, expected {MAGIC!r}")

        # Unpack header fields
        (
            _fmt_version,
            _timestamp,
            self._bitmap_offset,
            self._rank_offset,
            self._values_offset,
            self._land_cell_count,
            self._morton_block_offset,
            self._morton_dir_offset,
            self._morton_record_count,
            self._morton_block_count,
            self._admin_offset,
            self._name_offset,
            _reserved,
        ) = struct.unpack_from("<IIIIIIIIIIIIq", mm, 8)

        # Cache admin table length: each entry is 5 bytes (uint8 + uint16 + uint16)
        self._admin_entry_size = 5
        # Derive admin count from name_offset - admin_offset
        admin_section_len = self._name_offset - self._admin_offset
        self._admin_count = admin_section_len // self._admin_entry_size

        # Pre-build Morton directory as a Python list for bisect
        # Each directory entry is a uint32 (first morton code of that block)
        dir_count = self._morton_block_count
        dir_data = mm[self._morton_dir_offset: self._morton_dir_offset + dir_count * 4]
        self._directory = list(struct.unpack_from(f"<{dir_count}I", dir_data))

    # ------------------------------------------------------------------
    # Name table loading (zstd decompress once at startup)
    # ------------------------------------------------------------------

    def _load_name_tables(self):
        mm = self._mm
        name_data = bytes(mm[self._name_offset:])
        dctx = zstd.ZstdDecompressor()
        raw = dctx.decompress(name_data)
        tables = json.loads(raw.decode("utf-8"))
        self._countries = tables["countries"]
        self._adm1s = tables["adm1s"]
        self._adm2s = tables["adm2s"]

    # ------------------------------------------------------------------
    # Layer 0: coarse grid lookup
    # ------------------------------------------------------------------

    def _bitmap_bit(self, idx: int) -> bool:
        """Return True if bit `idx` is set in the bitmap."""
        byte_idx = self._bitmap_offset + idx // 8
        bit_idx = idx % 8
        return bool((self._mm[byte_idx] >> bit_idx) & 1)

    def _bitmap_rank(self, idx: int) -> int:
        """
        Return the number of set bits in the bitmap at positions 0..(idx-1).
        Uses the precomputed rank table (one entry per 512-bit block) plus
        a popcount of the partial block up to `idx`.
        """
        mm = self._mm
        block_idx = idx // 512
        # Read precomputed cumulative count before this block
        rank_entry_offset = self._rank_offset + block_idx * 4
        base_rank = struct.unpack_from("<I", mm, rank_entry_offset)[0]

        # Popcount the bits within this block from the block start to idx (exclusive)
        block_start_bit = block_idx * 512
        block_start_byte = self._bitmap_offset + block_start_bit // 8

        # Number of complete bytes before idx within this block
        bits_into_block = idx - block_start_bit
        complete_bytes = bits_into_block // 8
        remaining_bits = bits_into_block % 8

        partial = 0
        for i in range(complete_bytes):
            partial += bin(mm[block_start_byte + i]).count('1')

        if remaining_bits > 0:
            last_byte = mm[block_start_byte + complete_bytes]
            # Count only the low `remaining_bits` bits
            mask = (1 << remaining_bits) - 1
            partial += bin(last_byte & mask).count('1')

        return base_rank + partial

    def _grid_lookup(self, lat: float, lon: float) -> Optional[int]:
        """
        Layer 0 coarse grid lookup.
        Returns:
            None          — ocean (no land in this cell)
            SENTINEL_OCEAN  (0xFFFE) — ocean within a land-flagged coastal cell
            SENTINEL_BOUNDARY (0xFFFF) — boundary; caller must use Layer 1
            int (0-0xFFFD) — interior; direct admin_id
        """
        col = int((lon + 180.0) / GRID_CELL_DEG)
        row = int((90.0 - lat) / GRID_CELL_DEG)

        # Clamp to valid range
        col = max(0, min(GRID_COLS - 1, col))
        row = max(0, min(GRID_ROWS - 1, row))

        idx = row * GRID_COLS + col

        if not self._bitmap_bit(idx):
            return None  # ocean — fast path

        rank = self._bitmap_rank(idx)
        # Read value: rank-th uint16 in the values array (0-based)
        val_offset = self._values_offset + rank * 2
        value = struct.unpack_from("<H", self._mm, val_offset)[0]
        return value

    # ------------------------------------------------------------------
    # Layer 1: Morton boundary table lookup
    # ------------------------------------------------------------------

    def _block_search(self, morton: int) -> Optional[int]:
        """
        Search the Morton directory for the block that could contain `morton`,
        then linearly scan that block.
        Returns admin_id or None.
        """
        directory = self._directory
        if not directory:
            return None

        # Find the last block whose first key <= morton
        pos = bisect.bisect_right(directory, morton) - 1
        if pos < 0:
            return None

        # Linear scan within the block (at most 10 records)
        block_offset = self._morton_block_offset + pos * BLOCK_SIZE
        mm = self._mm

        for i in range(BLOCK_RECORDS):
            rec_offset = block_offset + i * RECORD_SIZE
            rec_morton, admin_id = struct.unpack_from("<IH", mm, rec_offset)

            # Records are sorted; if we've passed morton, stop
            if rec_morton > morton:
                break

            # Padding records at end of block have morton=0 (only if block not full)
            # A true zero morton (south pole, prime meridian) is valid but rare;
            # we handle it because we only stop on strictly greater.
            if rec_morton == morton:
                return admin_id

        return None

    def _morton_lookup(self, lat: float, lon: float) -> Optional[int]:
        """
        Layer 1 Morton boundary table lookup with parent fallback.
        Returns admin_id or None.
        """
        morton = compute_morton(lat, lon)
        admin_id = self._block_search(morton)
        if admin_id is not None:
            return admin_id

        # Parent fallback: mask out bottom 2 bits
        morton_parent = morton >> 2
        admin_id = self._block_search(morton_parent)
        return admin_id

    # ------------------------------------------------------------------
    # Admin table lookup
    # ------------------------------------------------------------------

    def _get_admin(self, admin_id: int) -> dict:
        """Return dict with country, adm1, adm2 strings for admin_id."""
        offset = self._admin_offset + admin_id * self._admin_entry_size
        c_idx, a1_idx, a2_idx = struct.unpack_from("<BHH", self._mm, offset)
        return {
            "country": self._countries[c_idx],
            "adm1": self._adm1s[a1_idx],
            "adm2": self._adm2s[a2_idx],
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, lat: float, lon: float) -> Optional[dict]:
        """
        Reverse geocode (lat, lon).

        Returns dict with keys 'country', 'adm1', 'adm2' for land points,
        or None for ocean / unclassified territory.

        Lat range: [-90, 90], Lon range: [-180, 180].
        """
        # Clamp inputs to valid range
        lat = max(-90.0, min(90.0, lat))
        lon = max(-180.0, min(180.0, lon))

        # Layer 0
        grid_value = self._grid_lookup(lat, lon)

        if grid_value is None:
            return None  # ocean

        if grid_value == SENTINEL_OCEAN:
            return None  # ocean within coastal cell

        if grid_value <= 0xFFFD:
            # Interior: fast path
            return self._get_admin(grid_value)

        # BOUNDARY (0xFFFF): fall through to Layer 1
        admin_id = self._morton_lookup(lat, lon)
        if admin_id is None:
            return None

        return self._get_admin(admin_id)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: python query.py <lat> <lon> [data_file]", file=sys.stderr)
        print("  Example: python query.py 48.8566 2.3522", file=sys.stderr)
        sys.exit(1)

    try:
        lat = float(sys.argv[1])
        lon = float(sys.argv[2])
    except ValueError:
        print("ERROR: lat and lon must be floating-point numbers.", file=sys.stderr)
        sys.exit(1)

    data_path = sys.argv[3] if len(sys.argv) == 4 else "z0_geo.bin"

    if not os.path.exists(data_path):
        print(f"ERROR: Data file not found: {data_path}", file=sys.stderr)
        print("  Run builder.py first to generate the data file.", file=sys.stderr)
        sys.exit(1)

    with ReverseGeocoder(data_path) as rg:
        result = rg.lookup(lat, lon)

    if result is None:
        print(f"({lat}, {lon}) -> ocean / unclassified")
    else:
        print(f"({lat}, {lon}) ->")
        print(f"  country: {result['country']}")
        print(f"  adm1:    {result['adm1']}")
        print(f"  adm2:    {result['adm2']}")


if __name__ == "__main__":
    main()
