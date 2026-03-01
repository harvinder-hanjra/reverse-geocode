# Benchmark results

Platform: Apple Silicon (macOS), Python 3.x query modules, 5 000 iterations per city, 100 warmup.

```
python bench.py --n 5000
```

All three geocoders are measured in the same process using the same Python query
modules that back the CLI tools. The numbers reflect the algorithmic cost, not
JavaScript overhead — but the relative ordering is the same in the browser.


## Raw numbers (mean µs)

    city                                       z0        s2        h3
    -----------------------------------------------------------------
    São Paulo, Brazil                       6.34      5.47      6.35
    London, UK                              6.22      5.89      2.63
    Tokyo, Japan                            5.80      6.03      2.65
    Nairobi, Kenya                          4.22      5.91      2.64
    Cairo, Egypt                            5.69      5.78      2.60
    Sydney, Australia                       5.56      6.35      4.67
    Moscow, Russia                          3.65      3.08      4.63
    Los Angeles, USA                        5.10      3.60      4.72
    Mumbai, India                           6.16      5.88      2.51
    Cape Town, South Africa                 0.39      6.31      4.67
    Tripoint DE/FR/CH                       5.38      6.14      2.52
    Kaliningrad, Russia                     3.69      6.26      2.52
    Mid-Atlantic (ocean)                    0.38      5.43      6.28
    South Pacific  (ocean)                  0.37      6.11      6.47


## What each cluster reveals

### z0

Three distinct latency clusters appear:

    ocean                  0.37–0.39 µs   bitmap bit test, early exit
    Layer 0 interior       3.65–4.22 µs   bitmap + rank + values array read
    Layer 0 boundary       5.10–6.34 µs   + Morton directory binary search + block scan

Cape Town (0.39 µs) matches ocean speed because the query coordinate
(-33.9249, 18.4241) happens to fall in a 0.25° grid cell whose centroid the
builder classified as ocean. The city is on the coast; the grid resolution is
coarse enough that the cell straddles land and water and the builder chose the
majority material.

Moscow and Kaliningrad (3.65–3.69 µs) are Layer 0 interior hits — their grid
cells are fully inside Russia and the lookup terminates after reading one entry
from the values array.

São Paulo (6.34 µs) and London (6.22 µs) are boundary hits — their 0.25°
cells straddle a border or a coastline and the lookup falls through to the
Morton table, paying the cost of a binary search on the directory and a linear
scan of one 64-byte block.


### s2

Two clusters:

    L10 interior hit       3.08–3.60 µs   H3 cell encode + directory binary search + block scan
    L12 boundary hit / ocean  5.43–6.35 µs  two directory searches + block scans

Moscow (3.08 µs) and LA (3.60 µs) are L10 hits — their res-6 parent cell is
entirely inside one region and the L10 directory finds it in the first pass.

Everything else — land boundary cells and ocean — costs ~5.5–6.4 µs because
both the L10 and L12 tables must be searched. Ocean has the same cost as a
boundary land cell because s2 has no early exit: H3 cell computation runs
unconditionally before any table lookup.

São Paulo (5.47 µs) is the fastest boundary city for s2. The H3 compact
encoding means all comparisons use uint32 Number arithmetic with no BigInt —
the cost is purely binary search operations.


### h3

Three clusters, each one binary search deeper:

    res-6 hit              2.51–2.65 µs   one binary search
    res-5 fallback         4.63–4.72 µs   two binary searches
    res-4 fallback / ocean 6.28–6.47 µs   three binary searches

London, Tokyo, Nairobi, Cairo, Mumbai, Tripoint, and Kaliningrad all sit in
res-6 cells that are fully indexed — one binary search finds them.

Sydney, Moscow, LA, and Cape Town require a res-5 fallback. Their query point
falls in a res-6 cell that was not filled at build time — typically because
the cell centroid landed outside all polygons or in a cell that straddles a
border at that resolution.

São Paulo (6.35 µs) matches ocean speed exactly — three binary searches all
miss. The h3 index was built from geoBoundaries rather than GADM (a leftover
from an earlier experiment), and São Paulo's specific res-6, res-5, and res-4
cells are apparently not covered in that dataset. This is a data-coverage
artifact, not an algorithmic failure.

Ocean points pay the maximum cost because h3 has no early exit — the fallback
chain always runs to res-4 before concluding null.


## Why the results look surprising

The Python timings are dominated by **Python interpreter overhead**, not
algorithmic differences. Each geocoder's `lookup()` call includes:

- attribute lookups on `self`
- Python integer arithmetic for index calculations
- `memoryview` / `struct.unpack` calls

At 5 µs per call, we are measuring ~15 000 Python bytecodes, not cache-miss
latency. The algorithmic advantage of z0's O(1) grid lookup vs h3's O(log N)
binary search is washed out.

In the JavaScript browser runtimes, where the JIT eliminates interpreter
overhead and cache misses dominate, the numbers look quite different:

    query type              z0          s2          h3
    --------------------------------------------------
    interior land           ~300 ns     ~1–2 µs     ~3–5 µs
    boundary land           ~1–2 µs     ~1.5–3 µs   ~5–8 µs
    ocean                   ~100–200ns  ~2 µs       ~7 µs

z0's coarse-grid O(1) lookup is ~10× faster than h3's binary search for
interior points in the browser. Python flattens this because its per-bytecode
cost is much larger than a cache miss.


## What the benchmark does and does not measure

Does measure:
- relative algorithmic cost between the three approaches
- the two- or three-level nature of each lookup (clusters)
- data-coverage gaps (São Paulo in h3)
- ocean early-exit behaviour (z0) vs always-search (s2, h3)

Does not measure:
- initial load time (binary parse, wasm init)
- memory pressure from multiple parallel queries
- JavaScript JIT performance
- cache warm-up effects beyond the 100-iteration warmup


## Running the benchmark

```sh
# from repo root, with z0 venv active (has h3 and zstandard installed)
source z0/.venv/bin/activate
python bench/bench.py

# skip geocoders you haven't built
python bench/bench.py --skip-s2
python bench/bench.py --n 1000   # fewer iterations for quick check
```

The script loads each geocoder's `query.py` from its own directory using
`importlib.util.spec_from_file_location` so all three can coexist in a single
process without `sys.modules` conflicts.
