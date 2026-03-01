## The Problem

**Input:** A latitude and longitude. Any point on Earth's surface.

**Output:** The administrative hierarchy that point falls inside: country, first-level subdivision (state/province), second-level subdivision (district/county). Or null if it's ocean/uninhabited.

**Constraints:**

- Offline forever. No network calls at query time. No external services.
- Runs on 512 MB RAM (t2.nano). Must coexist with OS + runtime.
- Sub-millisecond lookup. Every query, not just average.
- Worldwide coverage. ~195 countries, ~4,000 ADM1 regions, ~40,000 ADM2 regions.
- Static data. Updated at most annually. Build step can be expensive. Runtime must be cheap.
- Single deployable artifact. One binary file for data + one executable. No databases.

---

## First Principles

What does this problem actually reduce to?

A point query against ~40,000 non-overlapping polygons that tile the Earth's land surface. At query time, you need to answer: which polygon contains this point?

There are exactly three families of approach to pre-answer this question:

**Candidate 1: Point-in-polygon at runtime.**
Store the polygons. For each query, test containment. Accelerate with a spatial index (R-tree, KD-tree) to narrow candidates, then do the geometry test.

Kill it: a complex ADM2 boundary polygon can have thousands of vertices. `shapely.contains()` on such a polygon costs 100–5,000 µs. Even with a spatial index narrowing to 1–3 candidates, you're at 200–10,000 µs worst case. Violates the sub-millisecond constraint on complex boundaries. Also, storing 40K polygons with full vertex data is tens to hundreds of MB of geometry alone. Killed.

**Candidate 2: Rasterize to a regular grid.**
Precompute a 2D pixel grid covering the Earth. Each pixel stores the polygon ID it falls in. Query = convert (lat, lon) to pixel coordinate, read the value. O(1).

Test it: What resolution? To resolve ADM2 boundaries you need ~1 km pixels. Earth's surface is ~510M km². At 1 km resolution that's ~510M pixels. At 2 bytes per pixel (enough for 40K polygon IDs), that's ~1 GB. Doesn't fit on a nano. Coarser resolution (5 km) gets you ~20M pixels = ~40 MB, but misses small ADM2 regions entirely and gets boundaries wrong by 5 km. The fundamental tension: accuracy scales quadratically with storage. You can't have both fine resolution and small files with a uniform grid.

Can we fix it? RLE compression (like kno10) helps a lot — ocean and desert rows compress to almost nothing, dense areas don't compress much. kno10 gets city-level accuracy into ~49 MB. But you lose hierarchy (it's a flat mapping), and the resolution is fixed globally — you burn storage on Sahara rows that are 99% empty at the same pixel density as Tokyo. No adaptive resolution. Also, boundaries are staircase-approximated at pixel edges — no way to be more precise without quadrupling storage. This works, but it's not the smallest or most elegant. Keep it on the bench.

**Candidate 3: Hierarchical spatial index with precomputed classification.**
Tile the Earth into variable-sized cells using a hierarchical spatial index. Classify each cell with the polygon it falls inside. Store only cells that contain land. Query = convert (lat, lon) to cell ID, look up the classification.

This is the H3 approach. Why does it survive where the grid didn't?

- **Sparsity.** Only ~30% of Earth is land. Of that, only populated land needs fine resolution. H3 at res 5 has ~2M cells on land. At res 6, ~14M on land — but you only use res 6 where population justifies it. So you store ~1.5–2.5M cells total instead of 20–500M pixels.
- **Hierarchy is built in.** If a res 6 cell isn't in your table, compute its res 5 parent (one bitshift operation, ~nanoseconds). Instant fallback, no second data structure.
- **Uniform cell area.** Unlike lat/lon grids, H3 hexagons are roughly equal area everywhere. No wasted storage at poles.

Can I break it? The attack vector is boundary accuracy. H3 `polygon_to_cells` classifies a cell based on whether its centroid falls inside the polygon. A cell straddling a boundary gets assigned to whichever side claims the centroid. At res 5 (253 km²), this means a ~9 km radius of uncertainty at every ADM2 boundary. At res 6 (~36 km²), ~3.4 km. For a geocoder, being wrong by 3 km at a district border is acceptable — it's better than every KD-tree nearest-city approach, and the same accuracy class as the 49 MB raster. Real users are almost never standing exactly on a boundary line querying your geocoder.

Can I make it simpler? I could use S2 cells instead of H3. S2 has a cleaner hierarchy (quad-tree, every cell has exactly 4 children) and the cell ID encodes the hierarchy in the bit pattern directly. But S2 cells are quads, not hexagons — less uniform area, more distortion at faces. And the Go/Rust/Python H3 libraries are more mature for polygon-fill operations. The practical difference is minimal. H3 wins on ecosystem, not theory.

**Candidate 3 survives. Hierarchical spatial index with precomputed sparse classification is the shape.**

---

## Now: minimum storage within this shape

The record is: `(cell_id → admin_metadata)`.

**Cell ID storage:** H3 index is a uint64. Can we compress it?

H3 indexes encode resolution, base cell, and the hierarchy path. For a fixed resolution, many bits are redundant (resolution bits are identical for all entries, base cell repeats for neighboring cells). We could delta-encode sorted cell IDs — adjacent H3 cells in Morton-curve order have small deltas.

Test: is it worth it? 2.5M cells × 8 bytes = 20 MB. Delta-encoding + varint could cut this to ~12–15 MB. But it kills binary search — you'd need to decompress sequentially or build a secondary index, adding complexity and latency. The 5–8 MB saved isn't worth losing O(log N) random access. Keep raw uint64.

**Metadata storage:** What's the minimum bits per cell?

- ~195 countries → 8 bits (256 max)
- Max ADM1 per country: Russia ~85, USA 56 (incl. territories) → 8 bits (256 max)
- Max ADM2 per country: USA ~3,200, China ~2,800 → 12 bits is tight. 16 bits (65,536) is safe forever.
- Total: 8 + 8 + 16 = 32 bits = 4 bytes = uint32. Perfect natural alignment.

**Per-record: 12 bytes.** 2.5M records = 30 MB.

**Name tables:** ~195 country names + ~4,000 state names + ~40,000 district names. Average name ~15 bytes. Raw: ~660 KB. Zstd compressed: ~200 KB. Negligible.

**Total file: ~30 MB.** This is the theoretical floor for this approach at this accuracy level. You can't go lower without dropping resolution (losing ADM2 accuracy) or dropping cells (losing coverage).

---

## Minimum latency within this shape

Query path:

1. `lat, lon → H3 cell ID at res 6`. This is a fixed mathematical transform. ~5–15 µs in compiled code. No way to make it faster without rewriting H3 internals. It's pure trig + integer packing.

2. `Binary search for cell ID in sorted array of 2.5M entries`. log₂(2.5M) = 21 comparisons. Each comparison touches one 12-byte record. At ~3–5 ns per comparison (L1/L2 cache hit), that's ~60–100 ns. Even with cache misses from mmap page faults, each fault is ~1 µs, and binary search touches at most ~5–6 distinct cache lines across the 30 MB file. Worst case: ~5 µs. Typical: <200 ns.

3. **Fallback if miss:** Compute parent cell (one bitwise operation, ~ns). Second binary search. Same cost. At most 2 fallback levels (res 6 → 5 → 4), so worst case is 3 binary searches = ~15 µs.

4. `Unpack uint32 → three offsets. Look up name strings.` Bitwise ops + small table lookup. ~100 ns.

**Total worst case: ~20–35 µs hot, ~80–200 µs cold (mmap page faults).** The theoretical floor is dominated by the H3 coordinate transform. You can't beat ~5 µs without a fundamentally different spatial index, and nothing available is faster for this cell size.

---

## The architecture, from the problem up

```
BUILD TIME (once, expensive, big machine):
  geoBoundaries polygons
       ↓
  H3 polygon_to_cells (res 5 global + res 6 population-gated)
       ↓
  Assign packed uint32 metadata per cell
       ↓
  Sort by H3 index
       ↓
  Write: [header][sorted 12-byte records][zstd name tables]
       ↓
  Output: single 25-30 MB binary file

QUERY TIME (cheap, t2.nano):
  (lat, lon)
       ↓
  h3.LatLngToCell(res=6)        ← 5-15 µs
       ↓
  binary search in mmap'd file  ← <1 µs hot
       ↓
  found? → unpack uint32 → name lookup → return
  not found? → parent(res=5) → binary search → return
  not found? → parent(res=4) → binary search → return
  not found? → return null (ocean)
```

**File format:**

```
Offset 0:    [8 bytes]  Magic "LKHA0001"
Offset 8:    [4 bytes]  Version (uint32 LE)
Offset 12:   [4 bytes]  Record count N (uint32 LE)
Offset 16:   [4 bytes]  Name table offset (uint32 LE)
Offset 20:   [N × 12 bytes]  Sorted records
                 each: [uint64 LE h3_index] [uint32 LE packed_meta]
Offset 20+N×12: [variable]  Zstd-compressed name tables
```

**Packed meta (uint32):**

```
bits 31-24:  country_id      (8 bits, 0-255)
bits 23-16:  state_offset    (8 bits, 0-255, per country)
bits 15-0:   district_offset (16 bits, 0-65535, per country)
```

**Name table keys are flat per-country:** `(country_id) → name`, `(country_id, state_offset) → name`, `(country_id, district_offset) → name`.

---

That's the problem and the solution derived from the constraints. No inherited decisions, no cargo-culted libraries, no over-engineering. Every choice traces to a constraint.
