#!/usr/bin/env python3
"""
convert_rgeo4.py — RGEO0003 → RGEO0004

RGEO0004 replaces the flat Morton-sorted array with a palette+delta grouped
stream, one group per boundary coarse cell.  Admin/name tables are dropped
(the browser reads names from adm2_render.geojson, which is already loaded
for map rendering).

Typical result: 20 MB → ~4 MB  (5× smaller).

Usage:
    python convert_rgeo4.py [in.bin] [out.bin]

RGEO0004 binary layout
──────────────────────
Header (64 bytes):
  [0:8]   "RGEO0004"
  [8:12]  version u32 = 1
  [12:16] bitmap_off u32
  [16:20] rank_off u32
  [20:24] values_off u32      ← packed u24 array (3 bytes each)
  [24:28] land_cells u32
  [28:32] bndry_idx_off u32   ← u32 per boundary cell (stream-relative offsets)
  [32:36] stream_off u32
  [36:40] bndry_count u32
  [40:64] reserved

Sections:
  bitmap       same as RGEO0003 (1 bit per 0.25° cell, land=1)
  rank_table   same as RGEO0003 (cumulative popcount every 512 bits)
  values       land_cells × 3 bytes (u24 LE)
                 0xFFFFFF = BOUNDARY sentinel
                 0xFFFFFE = OCEAN sentinel (coastal cell, no polygon)
                 else     = admin_id (0 … 355685)
  bndry_index  bndry_count × u32 (byte offset into stream per boundary cell,
               in land-rank order — same order as BOUNDARY entries in values)
  stream       concatenated groups, one per boundary coarse cell

Group format:
  base_lq    u16 LE          quantized latitude index of cell's south edge
  base_aq    u16 LE          quantized longitude index of cell's west edge
  pal_size   u8              palette entries (0 = empty group)
  rec_count  u8              records in this group
  palette    pal_size × u24  admin_ids (LE)
  keys       rec_count × u8  local key = ((lq - base_lq) << 4) | (aq - base_aq)
                               sorted ascending; lq_off ∈ [0,6], aq_off ∈ [0,3]
  idxs       packed bit array
               pal_size == 1 → 0 bytes (index always 0)
               pal_size ∈ [2,4]  → ceil(rec_count × 2 / 8) bytes, 2 bits/idx
               pal_size ∈ [5,16] → ceil(rec_count × 4 / 8) bytes, 4 bits/idx
             MSB-first packing within each byte

Query (RGEO0004):
  1. Coarse grid lookup  →  97 % of queries done in O(1)
  2. BOUNDARY cell  →  bndry_rank via bndry_index, decode group, scan ≤18 recs
"""

import struct
import sys
import os

import numpy as np

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# ── Constants ─────────────────────────────────────────────────────────────────

MAGIC_IN  = b"RGEO0003"
MAGIC_OUT = b"RGEO0004"
HEADER_SIZE  = 64
GRID_COLS    = 1440
GRID_ROWS    = 720
GRID_CELLS   = GRID_COLS * GRID_ROWS
GRID_CELL_DEG = 0.25
MORTON_STEPS  = 4096
BLOCK_RECORDS = 8
BLOCK_SIZE    = 64
RECORD_SIZE   = 8

SENTINEL_BOUNDARY_V3 = 0xFFFFFFFF
SENTINEL_OCEAN_V3    = 0xFFFFFFFE
SENTINEL_BOUNDARY_V4 = 0xFFFFFF
SENTINEL_OCEAN_V4    = 0xFFFFFE


# ── Morton helpers (vectorised) ───────────────────────────────────────────────

def _spread_np(v):
    """Spread 12-bit values into 24-bit interleave positions (numpy uint32)."""
    v = v.astype(np.uint32)
    v = (v | (v << np.uint32(8)))  & np.uint32(0x00FF00FF)
    v = (v | (v << np.uint32(4)))  & np.uint32(0x0F0F0F0F)
    v = (v | (v << np.uint32(2)))  & np.uint32(0x33333333)
    v = (v | (v << np.uint32(1)))  & np.uint32(0x55555555)
    return v


def _compact(v: int) -> int:
    """Inverse of spread: extract bits from even positions (= recover lq from Morton)."""
    v = v & 0x55555555
    v = (v | (v >> 1))  & 0x33333333
    v = (v | (v >> 2))  & 0x0F0F0F0F
    v = (v | (v >> 4))  & 0x00FF00FF
    v = (v | (v >> 8))  & 0x0000FFFF
    return v


def lace_outer(lqs, aqs):
    """
    Compute Morton codes for all (lq, aq) combinations.
    lqs, aqs: 1-D uint32 arrays.
    Returns 2-D uint32 array of shape (len(lqs), len(aqs)).
    """
    sl = _spread_np(lqs)          # (n_lq,)
    sa = _spread_np(aqs)          # (n_aq,)
    return (sl[:, None] | (sa[None, :] << np.uint32(1))).astype(np.uint32)


# ── Bit packing (MSB-first within each byte) ──────────────────────────────────

def pack_idxs(idxs, idx_bits):
    """Pack list of idx values, idx_bits wide each, MSB-first."""
    result  = bytearray()
    cur     = 0
    bit_pos = 8          # bits remaining in cur byte
    for v in idxs:
        for i in range(idx_bits - 1, -1, -1):
            bit_pos -= 1
            cur |= ((v >> i) & 1) << bit_pos
            if bit_pos == 0:
                result.append(cur)
                cur, bit_pos = 0, 8
    if bit_pos < 8:
        result.append(cur)
    return bytes(result)


# ── Group encoder ─────────────────────────────────────────────────────────────

def encode_group(lq_lo: int, aq_lo: int, records):
    """
    Encode a list of (morton:int, admin_id:int) for one boundary coarse cell.

    lq_lo, aq_lo  — quantized coordinates of the cell's south-west corner.
    records       — list of (morton, admin_id); need not be pre-sorted.

    Group layout (header 6 bytes):
      base_lq u16 LE  base_aq u16 LE  pal_size u8  rec_count u8
      palette  pal_size × u24
      keys     rec_count × u8   key = ((lq-lq_lo)<<4)|(aq-aq_lo), sorted asc
      idxs     packed bits (MSB-first; 0/2/4 bits per record)
    """
    if not records:
        return struct.pack('<HHBB', lq_lo, aq_lo, 0, 0)   # 6-byte empty group

    # Build palette (order of first appearance)
    seen, palette = {}, []
    for _, aid in records:
        if aid not in seen:
            seen[aid] = len(palette)
            palette.append(aid)

    pal_size = len(palette)
    if pal_size > 16:
        palette  = palette[:16]
        pal_size = 16
        seen     = {aid: i for i, aid in enumerate(palette)}
    idx_bits = 0 if pal_size <= 1 else (2 if pal_size <= 4 else 4)

    # Compute local keys and sort by key
    keyed = []
    for m, aid in records:
        lq  = _compact(m)
        aq  = _compact(m >> 1)
        key = ((lq - lq_lo) << 4) | (aq - aq_lo)
        assert 0 <= key <= 255, f"key out of range: {key} (lq={lq} lq_lo={lq_lo} aq={aq} aq_lo={aq_lo})"
        keyed.append((key, seen.get(aid, 0)))
    keyed.sort()

    rec_count = len(keyed)
    assert rec_count <= 255

    keys      = bytes(k for k, _ in keyed)
    idxs      = [i for _, i in keyed]
    hdr       = struct.pack('<HHBB', lq_lo, aq_lo, pal_size, rec_count)
    pal_bytes = b''.join(struct.pack('<I', a)[:3] for a in palette)
    idx_bytes = pack_idxs(idxs, idx_bits) if idx_bits > 0 else b''

    return hdr + pal_bytes + keys + idx_bytes


# ── Main conversion ───────────────────────────────────────────────────────────

def main():
    in_path  = sys.argv[1] if len(sys.argv) > 1 else 'z0_geo.bin'
    out_path = sys.argv[2] if len(sys.argv) > 2 else 'z0_geo_v4.bin'

    print(f"Reading {in_path} …")
    with open(in_path, 'rb') as fh:
        raw = fh.read()

    if raw[:8] != MAGIC_IN:
        sys.exit(f"Expected RGEO0003, got {raw[:8]!r}")

    (_, _,
     bitmap_off, rank_off, values_off, land_cells,
     morton_block_off, morton_dir_off, morton_rec_count, morton_block_count,
     admin_off, name_off, _
    ) = struct.unpack_from('<IIIIIIIIIIIIq', raw, 8)

    print(f"  land cells: {land_cells:,}  Morton records: {morton_rec_count:,}")

    # ── Extract sections ──────────────────────────────────────────────────────
    bitmap_bytes  = (GRID_CELLS + 7) // 8
    n_rank_blocks = (GRID_CELLS + 511) // 512
    bitmap   = raw[bitmap_off  : bitmap_off  + bitmap_bytes]
    rank_raw = raw[rank_off    : rank_off    + n_rank_blocks * 4]
    values_u32 = np.frombuffer(
        raw[values_off : values_off + land_cells * 4], dtype=np.uint32
    ).copy()

    # ── Extract Morton records ────────────────────────────────────────────────
    print("Extracting Morton records …")
    block_data = raw[morton_block_off : morton_block_off + morton_block_count * BLOCK_SIZE]
    blk = np.frombuffer(block_data, dtype=np.uint8).reshape(morton_block_count, BLOCK_SIZE)
    rec = np.ascontiguousarray(blk[:, : BLOCK_RECORDS * RECORD_SIZE].reshape(-1, RECORD_SIZE))
    mortons = np.frombuffer(np.ascontiguousarray(rec[:, :4]).tobytes(),
                            dtype=np.uint32)[:morton_rec_count].copy()
    admins  = np.frombuffer(np.ascontiguousarray(rec[:, 4:]).tobytes(),
                            dtype=np.uint32)[:morton_rec_count].copy()
    print(f"  {len(mortons):,} records")

    # ── Identify boundary cells (in bitmap / land-rank order) ─────────────────
    print("Identifying boundary cells …")
    bm_arr = np.frombuffer(bitmap, dtype=np.uint8)
    bits   = np.unpackbits(bm_arr, bitorder='little')[:GRID_CELLS]
    cumsum = np.cumsum(bits, dtype=np.int32)
    land_rank_of_cell = np.where(bits == 1, cumsum - 1, -1).astype(np.int32)

    boundary_cells = []   # (row, col, land_rank)
    for idx in range(GRID_CELLS):
        lr = int(land_rank_of_cell[idx])
        if lr >= 0 and int(values_u32[lr]) == SENTINEL_BOUNDARY_V3:
            row, col = divmod(idx, GRID_COLS)
            boundary_cells.append((row, col, lr))
    print(f"  {len(boundary_cells):,} boundary cells")

    # ── Encode groups ─────────────────────────────────────────────────────────
    print("Encoding groups …")
    stream_parts  = []
    bndry_offsets = []     # stream-relative byte offsets
    stream_len    = 0
    total_recs    = 0
    pal_overflow  = 0

    iterator = (tqdm(boundary_cells, unit='cell', smoothing=0.1)
                if tqdm else boundary_cells)

    for row, col, _ in iterator:
        bndry_offsets.append(stream_len)

        lat_hi = 90.0 - row * GRID_CELL_DEG
        lat_lo = 90.0 - (row + 1) * GRID_CELL_DEG
        lon_lo = -180.0 + col  * GRID_CELL_DEG
        lon_hi = -180.0 + (col + 1) * GRID_CELL_DEG

        lq_lo = max(0, int((lat_lo + 90.0) / 180.0 * MORTON_STEPS))
        lq_hi = min(MORTON_STEPS - 1, int((lat_hi + 90.0) / 180.0 * MORTON_STEPS))
        aq_lo = max(0, int((lon_lo + 180.0) / 360.0 * MORTON_STEPS))
        aq_hi = min(MORTON_STEPS - 1, int((lon_hi + 180.0) / 360.0 * MORTON_STEPS))

        lqs = np.arange(lq_lo, lq_hi + 1, dtype=np.uint32)
        aqs = np.arange(aq_lo, aq_hi + 1, dtype=np.uint32)
        fine_m = lace_outer(lqs, aqs).ravel()   # all (lq,aq) combos

        records  = {}   # morton → admin_id (first match wins)
        for m32 in fine_m:
            m = int(m32)
            # Exact match
            pos = int(np.searchsorted(mortons, m32))
            if pos < len(mortons) and int(mortons[pos]) == m:
                records[m] = int(admins[pos])
                continue
            # Parent fallback (one level: morton >> 2)
            mp = np.uint32(m >> 2)
            pos = int(np.searchsorted(mortons, mp))
            if pos < len(mortons) and int(mortons[pos]) == int(mp):
                records[m] = int(admins[pos])

        sorted_recs = sorted(records.items())   # sorted by morton

        # Detect palette overflow (informational only; encode_group caps at 16)
        n_distinct = len({aid for _, aid in sorted_recs})
        if n_distinct > 16:
            pal_overflow += 1

        total_recs += len(sorted_recs)
        encoded = encode_group(lq_lo, aq_lo, sorted_recs)
        stream_parts.append(encoded)
        stream_len += len(encoded)

    if pal_overflow:
        print(f"  WARNING: {pal_overflow} groups had >16 distinct admin_ids (palette truncated)")
    print(f"  {total_recs:,} total fine records  |  stream {stream_len / 1024:.0f} KB")

    # ── Pack values as u24 ────────────────────────────────────────────────────
    print("Packing values as u24 …")
    values_u24 = bytearray(land_cells * 3)
    for i, v in enumerate(values_u32):
        v = int(v)
        if   v == SENTINEL_BOUNDARY_V3: vv = SENTINEL_BOUNDARY_V4
        elif v == SENTINEL_OCEAN_V3:    vv = SENTINEL_OCEAN_V4
        else:                           vv = v          # admin_id fits in 22 bits
        struct.pack_into('<I', values_u24, i * 3 - i, vv)   # 3-byte LE trick below

    # Re-pack properly: struct.pack '<I' gives 4 bytes, take first 3
    values_u24 = bytearray()
    for v in values_u32:
        values_u24 += struct.pack('<I', int(v) if int(v) < SENTINEL_OCEAN_V3
                                  else SENTINEL_BOUNDARY_V4 if int(v) == SENTINEL_BOUNDARY_V3
                                  else SENTINEL_OCEAN_V4)[:3]

    # ── Compute section offsets ───────────────────────────────────────────────
    bm_off_out   = HEADER_SIZE
    rk_off_out   = bm_off_out   + len(bitmap)
    val_off_out  = rk_off_out   + len(rank_raw)
    bidx_off_out = val_off_out  + len(values_u24)
    strm_off_out = bidx_off_out + len(bndry_offsets) * 4

    # ── Build header ──────────────────────────────────────────────────────────
    hdr = bytearray(HEADER_SIZE)
    hdr[0:8] = MAGIC_OUT
    struct.pack_into('<I', hdr,  8, 1)              # version
    struct.pack_into('<I', hdr, 12, bm_off_out)
    struct.pack_into('<I', hdr, 16, rk_off_out)
    struct.pack_into('<I', hdr, 20, val_off_out)
    struct.pack_into('<I', hdr, 24, land_cells)
    struct.pack_into('<I', hdr, 28, bidx_off_out)
    struct.pack_into('<I', hdr, 32, strm_off_out)
    struct.pack_into('<I', hdr, 36, len(boundary_cells))
    # [40:64] = 0 (reserved)

    # ── Write output ──────────────────────────────────────────────────────────
    print(f"Writing {out_path} …")
    with open(out_path, 'wb') as fh:
        fh.write(hdr)
        fh.write(bitmap)
        fh.write(rank_raw)
        fh.write(bytes(values_u24))
        for off in bndry_offsets:
            fh.write(struct.pack('<I', off))
        for part in stream_parts:
            fh.write(part)

    in_mb  = os.path.getsize(in_path)  / 1024 / 1024
    out_mb = os.path.getsize(out_path) / 1024 / 1024
    ratio  = in_mb / out_mb
    print(f"  RGEO0003  {in_mb:.1f} MB")
    print(f"  RGEO0004  {out_mb:.2f} MB  ({ratio:.1f}× smaller)")


if __name__ == '__main__':
    main()
