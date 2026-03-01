#!/usr/bin/env python3
"""
query4.py — RGEO0004 reverse geocoder query engine.

Loads z0_geo_v4.bin (RGEO0004) and provides reverse geocode lookups.
Returns admin_id integers (same IDs as the basemap GeoJSON feature IDs).

Usage:
    python query4.py <lat> <lon> [z0_geo_v4.bin]
"""

import os
import struct
import sys

import numpy as np

MAGIC           = b"RGEO0004"
GRID_COLS       = 1440
GRID_ROWS       = 720
GRID_CELL_DEG   = 0.25
MORTON_STEPS    = 4096
SENTINEL_BOUNDARY = 0xFFFFFF
SENTINEL_OCEAN    = 0xFFFFFE

# Pre-compute spread table for 12-bit Morton encoding
_SPREAD12 = np.zeros(4096, dtype=np.uint32)
for _v in range(4096):
    _r = 0
    for _i in range(12):
        _r |= ((_v >> _i) & 1) << (2 * _i)
    _SPREAD12[_v] = _r


class ReverseGeocoderV4:

    def __init__(self, data_path: str = "z0_geo_v4.bin"):
        with open(data_path, 'rb') as fh:
            self._data = fh.read()
        self._parse_header()
        self._build_tables()

    # ── Header ────────────────────────────────────────────────────────────────

    def _parse_header(self):
        data = self._data
        if data[:8] != MAGIC:
            raise ValueError(f"Bad magic: {data[:8]!r}, expected {MAGIC!r}")
        (version,
         bitmap_off, rank_off, values_off, land_cells,
         bndry_idx_off, stream_off, bndry_count,
        ) = struct.unpack_from('<IIIIIIII', data, 8)
        self._bitmap_off    = bitmap_off
        self._rank_off      = rank_off
        self._values_off    = values_off
        self._land_cells    = land_cells
        self._bndry_idx_off = bndry_idx_off
        self._stream_off    = stream_off
        self._bndry_count   = bndry_count

    # ── Startup tables ────────────────────────────────────────────────────────

    def _build_tables(self):
        data = self._data
        bitmap_bytes = (GRID_COLS * GRID_ROWS + 7) // 8

        # rank_cell[i] = land_rank for cell i, or -1 for ocean
        bm_arr = np.frombuffer(
            data[self._bitmap_off : self._bitmap_off + bitmap_bytes], dtype=np.uint8)
        bits = np.unpackbits(bm_arr, bitorder='little')[:GRID_COLS * GRID_ROWS].astype(np.int32)
        cumsum = np.cumsum(bits, dtype=np.int32)
        self._rank_cell = np.where(bits == 1, cumsum - 1, -1).astype(np.int32)

        # values: land_cells × u24 → uint32 array
        vals_raw = np.frombuffer(
            data[self._values_off : self._values_off + self._land_cells * 3],
            dtype=np.uint8,
        ).reshape(-1, 3)
        self._values_v = (
            vals_raw[:, 0].astype(np.uint32)
            | (vals_raw[:, 1].astype(np.uint32) << 8)
            | (vals_raw[:, 2].astype(np.uint32) << 16)
        )

        # boundary index: bndry_count × u32 (stream-relative byte offsets)
        self._bndry_idx = np.frombuffer(
            data[self._bndry_idx_off : self._bndry_idx_off + self._bndry_count * 4],
            dtype=np.uint32,
        )

        # bndry_rank_at_lr[land_rank] = boundary_rank, -1 if not a boundary cell
        is_bndry = (self._values_v == SENTINEL_BOUNDARY)
        cumsum_b = np.cumsum(is_bndry, dtype=np.int32)
        self._bndry_rank_at_lr = np.where(is_bndry, cumsum_b - 1, -1).astype(np.int32)

    # ── Group decoder ─────────────────────────────────────────────────────────

    def _decode_group(self, abs_off: int, lat: float, lon: float):
        """
        Decode the group starting at `abs_off` in self._data.
        Returns admin_id (int) if the query key matches a record, else None.
        """
        data = self._data

        # Header: base_lq u16 LE + base_aq u16 LE + pal_size u8 + rec_count u8
        base_lq   = data[abs_off]   | (data[abs_off+1] << 8)
        base_aq   = data[abs_off+2] | (data[abs_off+3] << 8)
        pal_size  = data[abs_off+4]
        rec_count = data[abs_off+5]
        off = abs_off + 6

        if pal_size == 0 or rec_count == 0:
            return None

        # Read palette
        palette = []
        for _ in range(pal_size):
            a = data[off] | (data[off+1] << 8) | (data[off+2] << 16)
            palette.append(a)
            off += 3

        # Compute local key for the query point
        lq = int((lat + 90.0) / 180.0 * MORTON_STEPS) & (MORTON_STEPS - 1)
        aq = int((lon + 180.0) / 360.0 * MORTON_STEPS) & (MORTON_STEPS - 1)
        lq_off = lq - base_lq
        aq_off = aq - base_aq
        if lq_off < 0 or lq_off > 15 or aq_off < 0 or aq_off > 15:
            return None
        query_key = (lq_off << 4) | aq_off

        # Scan keys (rec_count × u8, sorted ascending)
        idx_bits  = 0 if pal_size <= 1 else (2 if pal_size <= 4 else 4)
        matched_r = -1
        for r in range(rec_count):
            if data[off + r] == query_key:
                matched_r = r
                break
        off += rec_count  # advance past keys to idx bytes

        if matched_r < 0:
            return None
        if idx_bits == 0:
            return palette[0]

        # MSB-first bit extraction
        bit_start = matched_r * idx_bits
        byte_i    = bit_start >> 3
        shift     = 8 - (bit_start & 7) - idx_bits
        mask      = (1 << idx_bits) - 1
        idx       = (data[off + byte_i] >> shift) & mask
        return palette[idx] if idx < len(palette) else palette[0]

    # ── Public API ────────────────────────────────────────────────────────────

    def lookup(self, lat: float, lon: float):
        """
        Reverse geocode (lat, lon). Returns admin_id int or None for ocean.
        """
        lat = max(-90.0, min(90.0, lat))
        lon = max(-180.0, min(180.0, lon))

        col = max(0, min(GRID_COLS - 1, int((lon + 180.0) / GRID_CELL_DEG)))
        row = max(0, min(GRID_ROWS - 1, int((90.0 - lat)  / GRID_CELL_DEG)))
        idx = row * GRID_COLS + col

        land_rank = int(self._rank_cell[idx])
        if land_rank < 0:
            return None   # bitmap says ocean

        v = int(self._values_v[land_rank])
        if v == SENTINEL_OCEAN:
            return None   # coastal cell, no polygon
        if v < SENTINEL_OCEAN:
            return v      # interior: direct admin_id

        # BOUNDARY: decode the fine-resolution group
        bndry_rank = int(self._bndry_rank_at_lr[land_rank])
        if bndry_rank < 0:
            return None
        stream_off = int(self._bndry_idx[bndry_rank])
        return self._decode_group(self._stream_off + stream_off, lat, lon)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: python query4.py <lat> <lon> [data_file]", file=sys.stderr)
        sys.exit(1)

    lat  = float(sys.argv[1])
    lon  = float(sys.argv[2])
    path = sys.argv[3] if len(sys.argv) == 4 else "z0_geo_v4.bin"

    if not os.path.exists(path):
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)

    rg     = ReverseGeocoderV4(path)
    result = rg.lookup(lat, lon)

    if result is None:
        print(f"({lat}, {lon}) -> ocean / unclassified")
    else:
        print(f"({lat}, {lon}) -> admin_id {result}")


if __name__ == "__main__":
    main()
