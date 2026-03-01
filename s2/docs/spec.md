# Offline Reverse Geocoder — Technical Specification

## 1. Problem Statement

**Input:** A latitude and longitude — any point on Earth's surface.

**Output:** The administrative hierarchy containing that point: country, first-level subdivision (state/province), second-level subdivision (district/county). Returns null for ocean or unclassified territory.

### 1.1 Hard Constraints

| Constraint | Requirement |
|------------|-------------|
| Network | Fully offline. Zero network calls at query time. |
| Memory | Runs on 512 MB RAM (t2.nano). Must coexist with OS + runtime. |
| Latency | Sub-millisecond lookup, every query — worst case, not average. |
| Coverage | Worldwide: ~195 countries, ~4,000 ADM1 regions, ~40,000 ADM2 regions. |
| Data freshness | Static. Updated at most annually. Build step may be arbitrarily expensive. |
| Deployment | Single deployable artifact: one binary data file + one executable. No databases. |

---

## 2. Architecture Overview

The system uses Google's S2 Geometry library to decompose Earth's land surface into hierarchical quad-tree cells, pre-classifies each cell into an administrative region at build time, and serves lookups via cache-friendly block binary search at query time.

Two resolution tiers handle the accuracy/storage tradeoff:

- **Level 10** (~6 km cell edge): covers deep interior cells that fall entirely within a single ADM2 region. Resolves ~63% of land-area queries.
- **Level 12** (~1.5 km cell edge): covers boundary cells that straddle ADM2 borders. Resolves the remaining ~37%.

### 2.1 Why S2 Over H3

| Property | S2 | H3 |
|----------|----|----|
| Coordinate → cell ID | 0.3–1.5 µs (cube projection + Hilbert curve, pure integer ops) | 5–15 µs (icosahedral projection + aperture-7 hierarchy, heavy trig) |
| Parent computation | Bitmask: ~1 ns | Decode/re-encode: ~200–500 ns |
| Cell ID ordering | Hilbert curve — spatially nearby points have numerically nearby IDs (cache-friendly) | No guaranteed spatial locality in index order |
| Hierarchy | Clean quad-tree: every cell has exactly 4 children, encoded in bit pairs | Aperture-7: 7 children per cell, more complex indexing |
| Build-time tools | `S2RegionCoverer` natively answers "is this cell fully contained in polygon?" | `polygon_to_cells` uses centroid-in-polygon only |

S2's advantages are structural and apply at query time, where performance is constrained. H3's ecosystem advantage (more polygon-fill tooling) applies only at build time, where cost is unconstrained.

---

## 3. Data Model

### 3.1 Records

Each record maps an S2 cell to an administrative region:

```
┌─────────────────────┬──────────────┐
│  cell_id (uint32)   │ admin_id     │
│  4 bytes            │ (uint16)     │
│                     │ 2 bytes      │
└─────────────────────┴──────────────┘
         6 bytes per record
```

**Cell ID encoding:** At a fixed S2 level, the cell ID requires: 3 bits (face) + 2 × level bits (quad-tree path). Level 12 needs 27 bits; level 10 needs 23 bits. Both fit in a uint32. The level is implicit in which table the record belongs to.

**Admin ID encoding:** There are ~40,000 distinct (country, ADM1, ADM2) triples worldwide. A flat index into a lookup table of these triples needs 16 bits (uint16, max 65,535). This replaces the less efficient per-country offset encoding (which would require 32 bits for the same information).

### 3.2 Admin Lookup Table

A separate table maps each uint16 admin_id to its full hierarchy:

```
admin_id → {
  country_idx:  uint8   (0–255, covers ~195 countries)
  adm1_idx:     uint16  (0–65535, covers ~4,000 ADM1 regions)
  adm2_idx:     uint16  (0–65535, covers ~40,000 ADM2 regions)
}
```

Each entry is 5 bytes. 40,000 entries = ~200 KB.

### 3.3 Name Tables

Three string tables (countries, ADM1 names, ADM2 names) indexed by the corresponding idx fields. Stored zstd-compressed. Raw: ~660 KB. Compressed: ~200 KB.

---

## 4. File Format

A single binary file. All integers are little-endian.

```
OFFSET       SIZE          CONTENT
────────────────────────────────────────────────────────
0            8 bytes       Magic: "RGEO0001"
8            4 bytes       Version (uint32)
12           4 bytes       L10 record count (uint32)
16           4 bytes       L12 record count (uint32)
20           4 bytes       L10 directory offset (uint32)
24           4 bytes       L12 directory offset (uint32)
28           4 bytes       Admin table offset (uint32)
32           4 bytes       Name table offset (uint32)
36           —             (reserved / padding to 64-byte alignment)

--- L10 Block Array ---
  64-byte cache-line-aligned blocks
  Each block: up to 10 records of (uint32 cell_id, uint16 admin_id)
  ~800K records in ~80K blocks
  SIZE: ~5.0 MB

--- L10 Directory ---
  Array of uint32: first cell_id of each block
  ~80K entries × 4 bytes
  SIZE: ~320 KB

--- L12 Block Array ---
  Same structure as L10
  ~1.8M records in ~180K blocks
  SIZE: ~11.2 MB

--- L12 Directory ---
  ~180K entries × 4 bytes
  SIZE: ~720 KB

--- Admin Lookup Table ---
  40K entries × 5 bytes (country_idx, adm1_idx, adm2_idx)
  SIZE: ~200 KB

--- Name Tables (zstd-compressed) ---
  Country names, ADM1 names, ADM2 names
  SIZE: ~200 KB

────────────────────────────────────────────────────────
TOTAL: ~17.7 MB
```

### 4.1 Block Layout

Records within each tier are sorted by S2 cell ID. They are packed into 64-byte blocks (one cache line) containing up to 10 records (10 × 6 = 60 bytes, 4 bytes padding). The directory stores the first key of each block in a separate contiguous array, enabling a two-phase lookup:

1. Binary search the directory (small, fits in L2 cache).
2. Linear scan within the target block (single cache line read).

---

## 5. Query Algorithm

```
FUNCTION reverse_geocode(lat: float, lon: float) → AdminResult | null

  1. Compute S2 cell ID at level 12:
     cell_12 = s2.lat_lng_to_cell_id(lat, lon, level=12)
     COST: 0.3–1.5 µs

  2. Truncate to level 10:
     cell_10 = cell_12 & LEVEL_10_MASK
     COST: ~1 ns

  3. Block binary search L10 table:
     a. Binary search L10 directory for cell_10  → block index
     b. Linear scan within target block           → admin_id or miss
     COST: ~105 ns (directory in L2 cache)

  4. IF hit:
     → Look up admin_id in admin lookup table     → ~20 ns
     → Resolve name strings                       → ~50 ns
     → RETURN result
     (This path handles ~63% of queries)

  5. IF miss (boundary cell):
     Block binary search L12 table for cell_12
     COST: ~200 ns
     → Look up admin_id, resolve names
     → RETURN result

  6. IF miss in both tables:
     → RETURN null (ocean or unclassified)
```

### 5.1 Latency Budget

| Phase | Typical (hot cache) | Worst case (cold) |
|-------|--------------------:|------------------:|
| S2 cell computation | 0.5 µs | 1.5 µs |
| L10 directory search | 85 ns | 200 ns |
| L10 block scan | 20 ns | 50 ns |
| L12 search (37% of queries) | 200 ns | 500 ns |
| Admin + name lookup | 70 ns | 150 ns |
| **Total (interior cell, 63%)** | **~0.7 µs** | **~1.9 µs** |
| **Total (boundary cell, 37%)** | **~0.9 µs** | **~2.4 µs** |
| **Weighted average** | **~0.8 µs** | **~2.1 µs** |

All paths are sub-millisecond even in worst case.

---

## 6. Build Pipeline

The build runs on a large machine (no resource constraints). It executes once per data refresh (at most annually).

```
STEP 1: Ingest source polygons
  Source: geoBoundaries (CC-BY) or GADM
  Format: GeoJSON or Shapefile
  Output: ~40,000 (country, ADM1, ADM2) polygons

STEP 2: Assign admin IDs
  Deduplicate (country, ADM1, ADM2) triples
  Assign sequential uint16 admin_id to each
  Build admin lookup table and name tables

STEP 3: Compute S2 level 10 covering
  For each ADM2 polygon:
    Use S2RegionCoverer to compute level 10 cells
    Classify each cell:
      INTERIOR — cell is fully contained within polygon → emit to L10 table
      BOUNDARY — cell intersects polygon edge → mark for refinement

STEP 4: Refine boundary cells at level 12
  For each boundary cell from Step 3:
    Subdivide into 16 level 12 children (4^(12-10) = 16)
    Classify each child by centroid containment
    Emit to L12 table

STEP 5: Serialize
  Sort L10 records by cell ID
  Sort L12 records by cell ID
  Pack into 64-byte blocks
  Build directory arrays
  Zstd-compress name tables
  Write single binary file
```

### 6.1 Build Resource Estimates

| Step | Time (est.) | Peak memory |
|------|------------:|------------:|
| Polygon ingest | ~2 min | ~2 GB |
| S2 covering (level 10) | ~10–30 min | ~4 GB |
| Level 12 refinement | ~20–60 min | ~4 GB |
| Serialization | ~1 min | ~500 MB |
| **Total** | **~30–90 min** | **~4 GB** |

Parallelizable across polygons. An 8-core build machine should finish in under 30 minutes.

---

## 7. Boundary Accuracy Analysis

Boundary accuracy depends on the resolution of the finest tier used in that area.

**Level 12 cells have ~1.5 km edge length.** Centroid-based classification introduces up to ~0.75 km of error (half a cell edge) at any ADM2 boundary. This is the system's worst-case spatial error.

For context:
- GPS accuracy (civilian): ~5–10 m
- Typical geocoding precision: ~50–200 m
- ADM2 boundary data accuracy (geoBoundaries): ~100 m–1 km

The ~0.75 km boundary error is within the noise floor of the source data. Higher accuracy would require higher-fidelity polygon data, not finer S2 cells.

### 7.1 Optional: Level 14 refinement for critical boundaries

For applications requiring sub-kilometer boundary accuracy (e.g., international borders, disputed territories), a third tier at level 14 (~375 m cell edge) can be added for specific polygons. This adds ~2–5 MB for the most contested boundaries and brings worst-case error to ~190 m.

---

## 8. Runtime Requirements

| Resource | Usage |
|----------|-------|
| Data file | ~18 MB on disk, mmap'd at runtime |
| Resident memory | ~6 MB (L10 table hot, L12 paged in on demand) |
| CPU per query | <3 µs worst case |
| Dependencies | S2 geometry library (Go, Rust, C++, or Python bindings) |
| Concurrency | Lock-free. mmap'd read-only data supports unlimited concurrent readers. |

### 8.1 Deployment

The system consists of exactly two files:
1. `rgeo.bin` — the pre-built data file (~18 MB)
2. The query executable/library (statically linked with S2)

No database. No config. No network. Copy both files, run.

---

## 9. Comparison with Alternative Approaches

| Metric | Runtime PIP (R-tree) | Uniform raster | H3 flat array | **This design** |
|--------|---------------------:|---------------:|--------------:|----------------:|
| Data file size | ~200 MB | ~40–50 MB | ~30 MB | **~18 MB** |
| Query latency (typical) | 200–10,000 µs | ~1 µs | ~10–20 µs | **~0.8 µs** |
| Query latency (worst) | 10,000+ µs | ~1 µs | ~20–35 µs | **~2.4 µs** |
| Boundary accuracy | exact | ~5 km | ~3–9 km | **~1.5 km** |
| RAM footprint | 200+ MB | 40–50 MB | 30 MB | **~6 MB resident** |

---

## 10. Extension Points

**Multi-resolution gating by population density.** The build step can gate level 12 refinement on population data (e.g., WorldPop raster). Cells with zero population in a 10 km radius can remain at level 10. This reduces the L12 table by ~30–40% with no impact on queries that matter.

**Temporal versioning.** The file header includes a version field. Multiple data files (e.g., representing boundary changes over time) can coexist. The runtime selects the appropriate file by date.

**Custom attributes.** The admin lookup table can be extended with arbitrary fields (timezone, ISO 3166-2 code, population, etc.) without changing the core lookup structure. The per-record cost remains 6 bytes — only the lookup table grows.

**Streaming updates.** For applications that need intra-annual updates, a small delta file (containing only changed cells) can be overlaid on the base file at query time. The query checks the delta first (tiny, always in cache), then falls back to the base file.
