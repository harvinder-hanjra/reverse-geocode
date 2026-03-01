/**
 * z0.js — Offline reverse geocoder, browser runtime.
 *
 * Loads z0_geo.bin (RGEO0003) and exposes lookup(lat, lon) → admin_id | null.
 * TypedArray views that may be at non-4-byte-aligned offsets (rank table,
 * Morton directory) are copied via slice() to guarantee alignment.
 */

const GRID_COLS         = 1440;
const GRID_ROWS         = 720;
const SENTINEL_BOUNDARY = 0xFFFFFFFF;
const SENTINEL_OCEAN    = 0xFFFFFFFE;
const BLOCK_SIZE        = 64;
const BLOCK_RECORDS     = 8;
const RECORD_SIZE       = 8;  // uint32 morton + uint32 admin_id

// Byte popcount lookup table
const _PC8 = new Uint8Array(256);
for (let i = 1; i < 256; i++) _PC8[i] = (i & 1) + _PC8[i >> 1];

// Morton: spread bits of 16-bit int into even positions
function _spread(v) {
  v = (v | (v << 8)) & 0x00FF00FF;
  v = (v | (v << 4)) & 0x0F0F0F0F;
  v = (v | (v << 2)) & 0x33333333;
  v = (v | (v << 1)) & 0x55555555;
  return v >>> 0;
}

function _morton(lat, lon) {
  const lq = ((lat + 90)  / 180 * 4096) & 0xFFF;
  const aq = ((lon + 180) / 360 * 4096) & 0xFFF;
  return (_spread(lq) | (_spread(aq) << 1)) >>> 0;
}

export class Z0 {
  constructor(buffer) {
    const dv = new DataView(buffer);

    const magic = String.fromCharCode(...new Uint8Array(buffer, 0, 8));
    if (magic !== 'RGEO0003') throw new Error(`z0: bad magic "${magic}"`);

    // Header layout (uint32 LE, starting at byte 8):
    //  8  version    12  timestamp
    // 16  bitmapOff  20  rankOff   24  valuesOff  28  landCellCount
    // 32  mBlockOff  36  mDirOff   40  mRecCount  44  mBlockCount
    // 48  adminOff   52  nameOff   56  reserved(8)
    const bitmapOff   = dv.getUint32(16, true);
    const rankOff     = dv.getUint32(20, true);
    const valuesOff   = dv.getUint32(24, true);
    const landCells   = dv.getUint32(28, true);
    const mBlockOff   = dv.getUint32(32, true);
    const mDirOff     = dv.getUint32(36, true);
    const mBlockCount = dv.getUint32(44, true);

    const bitmapBytes   = Math.ceil(GRID_COLS * GRID_ROWS / 8);
    const numRankBlocks = Math.ceil(GRID_COLS * GRID_ROWS / 512);

    this._dv        = dv;
    this._mBlockOff = mBlockOff;

    // Bitmap: Uint8Array, no alignment constraint
    this._bitmap = new Uint8Array(buffer, bitmapOff, bitmapBytes);

    // Rank table: Uint32Array — offset may not be 4-byte aligned, so copy
    this._rank = new Uint32Array(buffer.slice(rankOff, rankOff + numRankBlocks * 4));

    // Grid values: Uint32Array — offset may not be 4-byte aligned, so copy
    this._values = new Uint32Array(buffer.slice(valuesOff, valuesOff + landCells * 4));

    // Morton directory: Uint32Array — copy to ensure alignment
    this._mDir = new Uint32Array(buffer.slice(mDirOff, mDirOff + mBlockCount * 4));

    // Pre-compute per-cell rank so lookup() needs no loop
    const cellCount = GRID_COLS * GRID_ROWS;
    const rankAtCell = new Int32Array(cellCount);
    let r = 0;
    for (let i = 0; i < cellCount; i++) {
      if ((this._bitmap[i >> 3] >> (i & 7)) & 1) rankAtCell[i] = r++;
      else rankAtCell[i] = -1;
    }
    this._rankAtCell = rankAtCell;
  }

  /** Returns admin_id or null for ocean/unclassified. */
  lookup(lat, lon) {
    lat = Math.max(-90,  Math.min(90,  lat));
    lon = Math.max(-180, Math.min(180, lon));

    // Layer 0: coarse 0.25° grid
    const col = Math.min(GRID_COLS - 1, Math.max(0, ((lon + 180) / 0.25) | 0));
    const row = Math.min(GRID_ROWS - 1, Math.max(0, ((90 - lat)  / 0.25) | 0));
    const idx = row * GRID_COLS + col;

    const rank = this._rankAtCell[idx];
    if (rank < 0) return null;  // ocean

    const v = this._values[rank];
    if (v === SENTINEL_OCEAN)    return null;
    if (v <= 0xFFFFFFFD)         return v;        // interior fast path

    // Layer 1: Morton boundary table
    const m = _morton(lat, lon);
    return this._blockSearch(m) ?? this._blockSearch(m >>> 2);
  }

  _blockSearch(m) {
    const dir = this._mDir;
    const n   = dir.length;
    if (!n) return null;

    let lo = 0, hi = n - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >>> 1;
      if (dir[mid] <= m) lo = mid; else hi = mid - 1;
    }
    if (dir[lo] > m) return null;

    const base = this._mBlockOff + lo * BLOCK_SIZE;
    const dv   = this._dv;
    for (let i = 0; i < BLOCK_RECORDS; i++) {
      const off = base + i * RECORD_SIZE;
      const rm  = dv.getUint32(off, true);
      if (rm > m)   break;
      if (rm === m) return dv.getUint32(off + 4, true);
    }
    return null;
  }
}

export async function loadZ0(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
  return createZ0(await res.arrayBuffer());
}

// ── RGEO0004 ──────────────────────────────────────────────────────────────────
// Palette+delta grouped stream. No admin/name tables (browser uses GeoJSON).
// Values are u24 (3 bytes); sentinels 0xFFFFFF=BOUNDARY, 0xFFFFFE=OCEAN.

export class Z0v4 {
  constructor(buffer) {
    const dv = new DataView(buffer);

    const magic = String.fromCharCode(...new Uint8Array(buffer, 0, 8));
    if (magic !== 'RGEO0004') throw new Error(`z0v4: bad magic "${magic}"`);

    // Header (all u32 LE):
    //  8  version   12  bitmapOff  16  rankOff   20  valuesOff
    // 24  landCells 28  bndryIdxOff 32  streamOff 36  bndryCount
    const bitmapOff   = dv.getUint32(12, true);
    const rankOff     = dv.getUint32(16, true);
    const valuesOff   = dv.getUint32(20, true);
    const landCells   = dv.getUint32(24, true);
    const bndryIdxOff = dv.getUint32(28, true);
    const streamOff   = dv.getUint32(32, true);
    const bndryCount  = dv.getUint32(36, true);

    const bitmapBytes = Math.ceil(GRID_COLS * GRID_ROWS / 8);

    this._buf8      = new Uint8Array(buffer);
    this._streamOff = streamOff;

    this._bitmap   = new Uint8Array(buffer, bitmapOff, bitmapBytes);
    this._valBytes = new Uint8Array(buffer, valuesOff, landCells * 3);
    this._bndryIdx = new Uint32Array(buffer.slice(bndryIdxOff, bndryIdxOff + bndryCount * 4));

    // rank_cell[i] = land_rank for grid cell i, -1 for ocean
    const cellCount  = GRID_COLS * GRID_ROWS;
    const rankAtCell = new Int32Array(cellCount);
    let r = 0;
    for (let i = 0; i < cellCount; i++) {
      rankAtCell[i] = (this._bitmap[i >> 3] >> (i & 7)) & 1 ? r++ : -1;
    }
    this._rankAtCell = rankAtCell;

    // bndryRankAtLR[land_rank] = boundary_rank (index into bndryIdx), -1 if not boundary
    const bndryRankAtLR = new Int32Array(landCells).fill(-1);
    const vb = this._valBytes;
    let br = 0;
    for (let lr = 0; lr < landCells; lr++) {
      const off3 = lr * 3;
      const v = vb[off3] | (vb[off3 + 1] << 8) | (vb[off3 + 2] << 16);
      if (v === 0xFFFFFF) bndryRankAtLR[lr] = br++;
    }
    this._bndryRankAtLR = bndryRankAtLR;
  }

  lookup(lat, lon) {
    lat = Math.max(-90,  Math.min(90,  lat));
    lon = Math.max(-180, Math.min(180, lon));

    const col = Math.min(GRID_COLS - 1, Math.max(0, ((lon + 180) / 0.25) | 0));
    const row = Math.min(GRID_ROWS - 1, Math.max(0, ((90 - lat)  / 0.25) | 0));
    const lr  = this._rankAtCell[row * GRID_COLS + col];
    if (lr < 0) return null;

    const off3 = lr * 3;
    const vb   = this._valBytes;
    const v    = vb[off3] | (vb[off3 + 1] << 8) | (vb[off3 + 2] << 16);
    if (v === 0xFFFFFE) return null;   // ocean sentinel
    if (v  <  0xFFFFFE) return v;      // interior admin_id

    // BOUNDARY: decode fine-resolution group
    const br = this._bndryRankAtLR[lr];
    if (br < 0) return null;
    return this._decodeGroup(this._streamOff + this._bndryIdx[br], lat, lon);
  }

  _decodeGroup(absOff, lat, lon) {
    const b = this._buf8;

    // Header: base_lq u16 LE + base_aq u16 LE + pal_size u8 + rec_count u8
    const baseLq   = b[absOff] | (b[absOff + 1] << 8);
    const baseAq   = b[absOff + 2] | (b[absOff + 3] << 8);
    const palSize  = b[absOff + 4];
    const recCount = b[absOff + 5];
    let off = absOff + 6;

    if (palSize === 0 || recCount === 0) return null;

    // Read palette (palSize × u24)
    const palette = new Uint32Array(palSize);
    for (let i = 0; i < palSize; i++) {
      palette[i] = b[off] | (b[off + 1] << 8) | (b[off + 2] << 16);
      off += 3;
    }

    // Compute local key for query point: key = (lq_off << 4) | aq_off
    const lq    = ((lat + 90)  / 180 * 4096) & 0xFFF;
    const aq    = ((lon + 180) / 360 * 4096) & 0xFFF;
    const lqOff = lq - baseLq;
    const aqOff = aq - baseAq;
    if (lqOff < 0 || lqOff > 15 || aqOff < 0 || aqOff > 15) return null;
    const queryKey = (lqOff << 4) | aqOff;

    const idxBits = palSize <= 1 ? 0 : palSize <= 4 ? 2 : 4;

    // Scan keys (rec_count × u8, sorted ascending)
    let matchedR = -1;
    for (let r = 0; r < recCount; r++) {
      if (b[off + r] === queryKey) { matchedR = r; break; }
    }
    off += recCount;  // advance to idx bytes

    if (matchedR < 0) return null;
    if (idxBits === 0) return palette[0];

    // MSB-first bit extraction
    const bitStart = matchedR * idxBits;
    const byteI    = bitStart >> 3;
    const shift    = 8 - (bitStart & 7) - idxBits;
    const mask     = (1 << idxBits) - 1;
    const idx      = (b[off + byteI] >> shift) & mask;
    return idx < palSize ? palette[idx] : palette[0];
  }
}

/** Auto-detect RGEO0003 vs RGEO0004 and return the right geocoder. */
export function createZ0(buffer) {
  const magic = String.fromCharCode(...new Uint8Array(buffer, 0, 8));
  return magic === 'RGEO0004' ? new Z0v4(buffer) : new Z0(buffer);
}
