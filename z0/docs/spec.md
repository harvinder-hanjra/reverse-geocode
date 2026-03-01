# Offline Reverse Geocoder — Technical Specification v2

## 0. What v1 Got Wrong

The previous spec chose S2 over H3 (correct) but then stopped. Three assumptions survived unchallenged:

1. **S2 is the fastest spatial index.** It isn't. S2's cell computation costs 300–1,500 ns because it involves cube-face projection, a nonlinear UV transform, and Hilbert curve index computation. A Morton code (bit-interleaved quantized lat/lon) does the same job in ~5 ns with pure integer arithmetic. S2's advantages — equal-area cells, clean hierarchy — are build-time properties. At query time, the only thing that matters is "turn (lat, lon) into a cell ID, fast."

2. **Every query must compute a cell ID.** It doesn't. Most of Earth's land area is deep interior — hundreds of kilometers from any administrative boundary. A coarse direct-mapped grid can resolve these queries in ~15 ns with zero spatial-index computation. Only boundary queries need fine-grained cells.

3. **Cell IDs must be stored explicitly.** They don't. A bitmap-indexed sparse grid encodes cell presence implicitly in the bitmap, storing only the 2-byte payload per cell. This cuts per-record cost from 6 bytes to 2 bytes for the coarse layer.

Fixing all three yields a system that is ~8× faster on average, ~28% smaller, and no less accurate.

---

## 1. Problem Statement

**Input:** Latitude and longitude (any point on Earth's surface).

**Output:** Administrative hierarchy — country, ADM1 (state/province), ADM2 (district/county) — or null for ocean/unclassified territory.

### 1.1 Hard Constraints

| Constraint | Requirement |
|------------|-------------|
| Network | Fully offline. Zero network calls at query time. |
| Memory | 512 MB total (t2.nano). Must coexist with OS + runtime. |
| Latency | Sub-millisecond every query — worst case, not average. |
| Coverage | Worldwide: ~195 countries, ~4,000 ADM1, ~40,000 ADM2 regions. |
| Data freshness | Static. Annual refresh. Build step may be arbitrarily expensive. |
| Deployment | Single binary data file + one executable. No databases. |

---

## 2. Architecture: Two-Layer Lookup

The core insight: **most queries are easy, and easy queries shouldn't pay the cost of hard ones.**

~55–60% of land-area queries fall deep inside an administrative region, far from any boundary. These can be answered by a coarse grid with no spatial-index computation at all. Only the ~40% of queries near boundaries need a fine-grained cell lookup.

```
(lat, lon)
    │
    ▼
┌──────────────────────────────────┐
│  LAYER 0: Coarse Grid (0.25°)   │  ← ~15 ns
│  Bitmap-indexed, ~762 KB        │
│  Resolves ~58% of land queries  │
└──────────┬───────────┬──────────┘
           │           │
       INTERIOR      BOUNDARY
       (hit)        (sentinel)
           │           │
           ▼           ▼
      Return      ┌────────────────────────────────┐
      admin_id    │  LAYER 1: Morton Boundary Table │  ← ~160 ns
                  │  Block-indexed, ~10.8 MB        │
                  │  Resolves remaining ~42%        │
                  └──────────┬─────────────────────┘
                             │
                         Return admin_id
                         (or null = ocean)
```

### 2.1 Why Morton Codes, Not S2

The previous spec used S2 cell IDs. S2 computes `(lat, lon) → cell_id` through: (1) lat/lon → XYZ unit sphere (sin, cos), (2) XYZ → cube face (comparisons), (3) face → UV projection (division + nonlinear transform), (4) UV → Hilbert curve index (bit interleaving with state machine). Cost: **300–1,500 ns** in compiled code.

A Morton code computes the same mapping through: (1) quantize lat, lon to fixed-point integers (multiply + truncate), (2) bit-interleave the two integers (single `PDEP` instruction on x86, or lookup table). Cost: **3–8 ns.**

Morton codes sacrifice two properties of S2 — equal-area cells (Morton cells in lat/lon space shrink near the poles) and optimal spatial locality (Hilbert curves have fewer large jumps than Z-order curves). Neither matters here:

- **Unequal area:** At 80°N, a Morton cell is ~5× smaller than at the equator. This means *more* cells (and higher accuracy) near the poles, not less. The overhead is negligible because almost no populated land exists above 75°N.
- **Worse locality:** Both Z-order and Hilbert-order curves feed into a binary search. Binary search performance depends on key distribution uniformity, not spatial locality. The ~15% locality difference between Z-order and Hilbert curves affects sequential scans, which this design never performs.

**Morton codes dominate S2 at query time. S2's advantages apply only to build-time polygon operations, where cost is unconstrained.**

### 2.2 Why a Coarse Grid, Not Just Morton

The Morton-code boundary table resolves any query in ~160 ns (cell computation + block search). But 58% of queries don't need it. A 0.25° grid (~28 km cells) can classify deep-interior points in ~15 ns because the lookup is two array indexes — no spatial math at all.

The coarse grid adds ~762 KB to the file. It saves 300+ ns on 58% of queries. The amortized cost: 762 KB buys a ~6× reduction in average query latency.

---

## 3. Layer 0: Bitmap-Indexed Coarse Grid

### 3.1 Structure

Resolution: 0.25° latitude × 0.25° longitude.
Grid dimensions: 1,440 columns × 720 rows = 1,036,800 cells.

Three data structures, stored contiguously:

```
┌─────────────────────────────────────────────────┐
│  BITMAP: 1 bit per cell (has land?)             │
│  1,036,800 bits = 129,600 bytes = 126.6 KB      │
├─────────────────────────────────────────────────┤
│  RANK TABLE: precomputed popcount every 512 bits│
│  129,600 / 64 = 2,025 entries × 4 bytes = ~8 KB │
├─────────────────────────────────────────────────┤
│  VALUES: uint16 per land cell, dense-packed     │
│  ~311,000 land cells × 2 bytes = ~622 KB         │
│  Value = admin_id (0x0000–0xFFFD)               │
│        | 0xFFFF = BOUNDARY (needs Layer 1)       │
│        | 0xFFFE = OCEAN within a land-flagged    │
│          cell (e.g. coastal cells with tiny land)│
└─────────────────────────────────────────────────┘
TOTAL: ~762 KB
```

### 3.2 Query Algorithm

```
FUNCTION grid_lookup(lat: f64, lon: f64) → uint16 | MISS

  col = floor((lon + 180.0) / 0.25)          // 1 multiply, 1 truncate
  row = floor((90.0 - lat) / 0.25)           // 1 multiply, 1 truncate
  idx = row × 1440 + col                     // 1 multiply, 1 add
  COST: ~3 ns

  IF bitmap[idx] == 0:
    RETURN MISS (ocean, no land in this cell)
  COST: ~2 ns (single byte read)

  rank = rank_table[idx / 512] + popcount(bitmap[rank_block_start..idx])
  value = values[rank]
  COST: ~8 ns (rank lookup + POPCNT + array index)

  RETURN value
  TOTAL: ~13–18 ns
```

### 3.3 Classification at Build Time

A coarse cell is classified as:

- **INTERIOR** (`admin_id`): The cell and all its immediate neighbors belong to the same ADM2 region. This ensures that even a point near the cell edge is correctly classified. Neighbor check adds one cell width (~28 km) of safety margin.
- **BOUNDARY** (`0xFFFF`): The cell or any neighbor straddles an ADM2 boundary.
- **OCEAN** (`0xFFFE`): Land exists in the cell but is too small or fragmented to classify reliably (e.g., tiny islands in a mostly-ocean cell).

The neighbor-check rule is critical: without it, a point 100 meters inside a coarse cell boundary could be on the wrong side of an ADM2 boundary that clips the cell corner. With it, the coarse grid never returns a wrong answer — only "I don't know" (BOUNDARY), which falls through to Layer 1.

---

## 4. Layer 1: Morton-Indexed Boundary Table

### 4.1 Morton Code Computation

Quantize lat/lon to 16-bit unsigned integers:

```
lat_q = floor((lat + 90.0) / 180.0 × 65536)   // uint16
lon_q = floor((lon + 180.0) / 360.0 × 65536)   // uint16
morton = interleave(lat_q, lon_q)                // uint32
```

The `interleave` operation maps two 16-bit integers to one 32-bit integer by alternating their bits. On x86-64 with BMI2, this is a single `PDEP` instruction (~1 cycle). On ARM or without BMI2, a 256-entry lookup table achieves the same result in ~3 ns.

**Resolution:** 16-bit quantization gives 65,536 steps per axis. Latitude resolution: 180° / 65,536 = 0.00275° ≈ 306 m. Longitude resolution at equator: 360° / 65,536 = 0.00549° ≈ 611 m. Effective cell size: ~306 m × 306–611 m. This is approximately equivalent to S2 level 13 (~375 m edge).

**Hierarchy:** The Morton code encodes a natural quad-tree. Masking out the bottom 2 bits gives the parent cell. Masking out 4 bits gives the grandparent. Cost: ~1 ns per level.

### 4.2 Storage: Block-Indexed Sorted Array

Records are sorted by Morton code. Each record is 6 bytes:

```
┌───────────────────┬──────────────┐
│  morton (uint32)  │  admin_id    │
│  4 bytes          │  (uint16)    │
│                   │  2 bytes     │
└───────────────────┴──────────────┘
```

Records are packed into 64-byte cache-line-aligned blocks. Each block holds 10 records (60 bytes, 4 bytes padding). A separate **directory** stores the first Morton code of each block:

```
┌──────────────────────────────────────────────────┐
│  BLOCK ARRAY: ~1.8M records in ~180K blocks      │
│  180,000 × 64 bytes = ~11.0 MB                   │
├──────────────────────────────────────────────────┤
│  DIRECTORY: first key of each block              │
│  180,000 × 4 bytes = ~720 KB                     │
└──────────────────────────────────────────────────┘
```

### 4.3 Query Algorithm

```
FUNCTION boundary_lookup(lat: f64, lon: f64) → uint16 | NULL

  morton = compute_morton(lat, lon)            // ~5 ns
  block = binary_search(directory, morton)     // ~100 ns (directory fits in L2)
  result = linear_scan(blocks[block], morton)  // ~20 ns (one cache line)

  IF hit:
    RETURN result.admin_id

  // Fallback: try parent Morton cell (mask bottom 2 bits)
  morton_parent = morton >> 2
  block = binary_search(directory, morton_parent)
  result = linear_scan(blocks[block], morton_parent)

  IF hit:
    RETURN result.admin_id

  RETURN NULL (ocean or unclassified)

  TOTAL: ~130 ns (hit) to ~260 ns (parent fallback)
```

### 4.4 Why Block Binary Search

Standard binary search over 1.8M × 6-byte records (10.8 MB) requires log₂(1.8M) = 21 comparisons. The first ~7 comparisons miss L3 cache, costing ~80 ns each. Realistic total: ~1.5–2.5 µs.

Block binary search splits this into:

1. **Binary search the directory** (720 KB, fits in L2/L3 after warmup): log₂(180K) = 17 comparisons × ~5 ns = ~85 ns.
2. **Linear scan within one block** (64 bytes, one cache line read): ~20 ns.

Total: **~105–130 ns** — approximately 15× faster than naive binary search on the full array.

---

## 5. Admin Metadata

### 5.1 Admin Lookup Table

Both layers return a `uint16 admin_id` (0–65,535). This indexes into a flat lookup table:

```
admin_id → {
  country_idx:  uint8    (0–255, covers ~195 countries)
  adm1_idx:     uint16   (0–65,535, covers ~4,000 ADM1 regions)
  adm2_idx:     uint16   (0–65,535, covers ~40,000 ADM2 regions)
}
```

Per entry: 5 bytes. 40,000 entries = **200 KB.** Accessed by direct index (one multiply + pointer add). Cost: ~5 ns.

### 5.2 Name Tables

Three arrays of null-terminated UTF-8 strings, indexed by `country_idx`, `adm1_idx`, `adm2_idx`:

| Table | Entries | Avg. length | Raw size | Zstd compressed |
|-------|--------:|------------:|---------:|----------------:|
| Countries | ~195 | ~12 bytes | ~2.3 KB | ~1.5 KB |
| ADM1 | ~4,000 | ~15 bytes | ~60 KB | ~25 KB |
| ADM2 | ~40,000 | ~15 bytes | ~600 KB | ~200 KB |
| **Total** | | | **~662 KB** | **~227 KB** |

Decompressed into memory at load time. Resident cost: ~662 KB (fits comfortably alongside the data file).

---

## 6. File Format

Single binary file. All integers little-endian.

```
OFFSET           SIZE          CONTENT
──────────────────────────────────────────────────────
0                8 bytes       Magic: "RGEO0002"
8                4 bytes       Format version (uint32)
12               4 bytes       Build timestamp (unix epoch, uint32)
16               4 bytes       Grid bitmap offset (uint32)
20               4 bytes       Grid rank table offset (uint32)
24               4 bytes       Grid values offset (uint32)
28               4 bytes       Grid land cell count (uint32)
32               4 bytes       Morton block array offset (uint32)
36               4 bytes       Morton directory offset (uint32)
40               4 bytes       Morton record count (uint32)
44               4 bytes       Morton block count (uint32)
48               4 bytes       Admin table offset (uint32)
52               4 bytes       Name table offset (uint32)
56               8 bytes       Reserved (zero)
────────────────────────────────────────────────────── 64 bytes header

--- Layer 0: Coarse Grid ---
  Bitmap:      126.6 KB   (1,036,800 bits, 64-byte aligned)
  Rank table:    8.1 KB   (2,025 × uint32)
  Values:      622.0 KB   (~311,000 × uint16)
  SUBTOTAL:    ~762 KB

--- Layer 1: Morton Boundary Table ---
  Block array:  11.0 MB   (~180,000 blocks × 64 bytes)
  Directory:   720.0 KB   (~180,000 × uint32)
  SUBTOTAL:    ~11.7 MB

--- Admin Lookup Table ---
  Entries:     200.0 KB   (40,000 × 5 bytes)

--- Name Tables (zstd-compressed) ---
  Compressed:  ~227 KB

──────────────────────────────────────────────────────
TOTAL FILE SIZE: ~12.9 MB
```

The entire file is mmap'd read-only at runtime. No parsing, no deserialization. The header offsets point directly into the mmap'd region.

---

## 7. Query: Full Path

```
FUNCTION reverse_geocode(lat: f64, lon: f64) → Result | null

  // ---- Layer 0: Coarse Grid ---- (~15 ns)
  col = floor((lon + 180.0) / 0.25)
  row = floor((90.0 - lat) / 0.25)
  idx = row × 1440 + col

  IF NOT bitmap[idx]:
    RETURN null                          // ocean (fast path)

  rank = rank_table[idx/512] + popcount(bitmap[aligned..idx])
  grid_value = values[rank]

  IF grid_value ≤ 0xFFFD:               // INTERIOR (58% of land queries)
    admin = admin_table[grid_value]
    RETURN {
      country: names.country[admin.country_idx],
      adm1:    names.adm1[admin.adm1_idx],
      adm2:    names.adm2[admin.adm2_idx]
    }
    TOTAL: ~20 ns

  IF grid_value == 0xFFFE:
    RETURN null                          // ocean within coastal cell

  // ---- Layer 1: Morton Table ---- (~160 ns)
  // grid_value == 0xFFFF → BOUNDARY
  morton = compute_morton(lat, lon)      // ~5 ns
  admin_id = block_binary_search(morton) // ~130 ns

  IF admin_id == NOT_FOUND:
    morton_parent = morton >> 2
    admin_id = block_binary_search(morton_parent)

  IF admin_id == NOT_FOUND:
    RETURN null

  admin = admin_table[admin_id]
  RETURN {
    country: names.country[admin.country_idx],
    adm1:    names.adm1[admin.adm1_idx],
    adm2:    names.adm2[admin.adm2_idx]
  }
  TOTAL: ~160–280 ns
```

---

## 8. Latency Analysis

| Query type | Fraction | Hot cache | Cold cache (mmap faults) |
|-----------|----------:|----------:|-------------------------:|
| Ocean (bitmap miss) | ~70% of all | ~8 ns | ~50 ns |
| Interior (grid hit) | ~58% of land | ~20 ns | ~200 ns |
| Boundary (morton hit) | ~38% of land | ~160 ns | ~800 ns |
| Boundary (parent fallback) | ~4% of land | ~280 ns | ~1,200 ns |
| **Weighted average (all queries)** | | **~35 ns** | **~250 ns** |
| **Weighted average (land only)** | | **~85 ns** | **~500 ns** |
| **Worst case (any single query)** | | **~280 ns** | **~1,200 ns** |

Every path is sub-millisecond, including worst-case cold-cache scenarios.

### 8.1 Comparison to v1

| Metric | v1 (S2 + flat array) | **v2 (grid + Morton)** | Improvement |
|--------|---------------------:|-----------------------:|------------:|
| File size | 17.7 MB | **12.9 MB** | **27% smaller** |
| Typical query (interior) | 700 ns | **20 ns** | **35× faster** |
| Typical query (boundary) | 900 ns | **160 ns** | **5.6× faster** |
| Weighted avg (land) | 800 ns | **85 ns** | **9.4× faster** |
| Worst case | 2,400 ns | **280 ns** | **8.6× faster** |
| Boundary accuracy | ~750 m | **~375 m** | **2× finer** |
| External dependencies | S2 geometry library | **None** | Simpler deploy |

---

## 9. Build Pipeline

Runs once per data refresh on a large build machine. No resource constraints.

```
STEP 1: Ingest Polygons
  Source:  geoBoundaries (CC-BY) or GADM
  Format: GeoJSON / Shapefile
  Output: ~40,000 (country, ADM1, ADM2) polygons with geometries

STEP 2: Assign Admin IDs
  Deduplicate (country, ADM1, ADM2) triples
  Assign sequential uint16 admin_id (0–39,999)
  Build admin lookup table + name string tables

STEP 3: Build Coarse Grid (Layer 0)
  For each 0.25° cell that intersects land:
    Set bitmap[cell] = 1
    Test: does this cell AND all 8 neighbors fall within a single ADM2 polygon?
      YES → values[rank] = admin_id (INTERIOR)
      NO  → values[rank] = 0xFFFF (BOUNDARY)
    If cell is land-flagged but too fragmented:
      values[rank] = 0xFFFE (OCEAN)
  Build rank table from bitmap

STEP 4: Build Morton Boundary Table (Layer 1)
  For each BOUNDARY coarse cell:
    Subdivide into Morton cells at 16-bit quantization (~306 m resolution)
    For each Morton cell:
      Determine centroid
      Point-in-polygon test against candidate ADM2 polygons
      Emit (morton_code, admin_id) record
  Sort all records by morton_code
  Pack into 64-byte blocks
  Build directory (first key per block)

STEP 5: Serialize
  Write header + all sections to single binary file
  Zstd-compress name tables
  Validate: spot-check 10,000 random land points against source polygons
```

### 9.1 Build Resource Estimates

| Step | Time (est.) | Peak memory |
|------|------------:|------------:|
| Polygon ingest | ~2 min | ~2 GB |
| Coarse grid classification | ~5 min | ~1 GB |
| Morton cell rasterization | ~30–90 min | ~4 GB |
| Sort + serialize | ~2 min | ~500 MB |
| Validation | ~1 min | ~200 MB |
| **Total** | **~40–100 min** | **~4 GB peak** |

Parallelizable across polygons. An 8-core machine should complete in under 30 minutes.

---

## 10. Boundary Accuracy

Accuracy is governed by the Morton cell resolution in Layer 1.

At 16-bit quantization, latitude resolution is 180° / 65,536 = 0.00275° ≈ **306 meters.** Centroid-based classification introduces up to half a cell edge of error: **~153 m worst case.**

For reference:

| Source | Typical accuracy |
|--------|:----------------:|
| Civilian GPS | 5–10 m |
| Geocoding APIs | 50–200 m |
| geoBoundaries source data | 100 m–1 km |
| **This system (Layer 1)** | **~153 m** |
| v1 spec (S2 level 12) | ~750 m |

The system's boundary accuracy now exceeds the accuracy of its source data. Further refinement would require higher-fidelity polygon sources, not finer cells.

---

## 11. Runtime Requirements

| Resource | Usage |
|----------|-------|
| Data file | ~12.9 MB on disk, mmap'd read-only |
| Resident memory | ~2 MB hot (grid + Morton directory), rest paged on demand |
| CPU per query | <300 ns worst case |
| External dependencies | **None.** Morton codes are ~30 lines of arithmetic. No S2, no H3. |
| Concurrency | Lock-free. Read-only mmap supports unlimited concurrent readers. |
| Startup | mmap() call. No parsing, no decompression (except name tables). Sub-millisecond. |

### 11.1 Deployment

Two files:

1. `rgeo.bin` — pre-built data file (~12.9 MB)
2. The query executable or library (statically compiled, no external dependencies)

Copy both files. Run. No configuration. No database. No network.

---

## 12. Robustness Properties

**The coarse grid never lies.** It returns INTERIOR only when the cell and all neighbors belong to the same region. A wrong classification is structurally impossible — it can only be overly cautious (marking an interior cell as BOUNDARY), which triggers a Layer 1 lookup that gives the correct answer.

**Layer 1 is centroid-classified.** A Morton cell that straddles a boundary is assigned to the region containing its centroid. This can be wrong by up to ~153 m. This is the system's only source of error, and it is bounded and predictable.

**No fallback chain deeper than 2.** Layer 0 → Layer 1 → parent fallback. Three lookups maximum. No unbounded recursion, no retry loops. Worst-case latency is deterministic.

**No floating-point comparison in the hot path.** Layer 0 uses integer arithmetic (multiply, truncate, index). Layer 1 uses integer Morton codes and integer binary search. The only floating-point operations are the initial quantization (two multiplies, two truncates). No epsilon issues, no NaN handling, no platform-dependent rounding.

---

## 13. Extension Points

**Population-gated refinement.** The build step can use WorldPop or similar data to skip Morton-cell generation in uninhabited boundary areas. Cells with zero population within 10 km remain classified at the coarse grid level. Estimated savings: 20–30% fewer Morton records (~2–3 MB).

**Sub-cell boundary encoding.** For boundary cells where a single straight-line boundary crosses the cell, the build step can encode the boundary position within the cell (axis + offset, ~8 bits). At query time, a single comparison determines which side of the boundary the point falls on, achieving effectively zero error. Applicable to ~80% of boundary cells. Adds ~1.8 MB to file. Reduces effective accuracy from ~153 m to ~5 m for straight-line boundaries.

**Custom attributes.** The admin lookup table can be extended with arbitrary fields (timezone, ISO 3166-2 code, population, calling code) by appending columns. The core lookup structure is unchanged. Per-record cost in Layers 0 and 1 remains 2 bytes — only the admin table grows.

**Delta updates.** A small overlay file containing only changed Morton cells can be applied at query time. The query checks the delta file first (tiny, always in L1 cache), then falls through to the base file. Enables sub-annual boundary updates without rebuilding the full dataset.

**Level-of-detail API.** Some callers need only the country. The query can short-circuit after Layer 0 for any cell where the country is unambiguous (true for ~85% of land area), avoiding the Layer 1 lookup entirely. Average latency for country-only queries: ~15 ns.
