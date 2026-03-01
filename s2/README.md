# s2 — Block binary search geocoder (H3-backed)

Binary format: **RGEO0001**
Builder: **Python** (`builder.py`)
UI: `ui/` (port 5175)

Despite the name "s2", this implementation does not use the S2 geometry
library. It uses **H3** internally. The name reflects the intended design
(S2 cell-based indexing) but the actual cells are H3 hexagons, not S2
quadrilaterals. The S2 Python bindings have historically been difficult to
install; h3 (`pip install h3`) works on all platforms without any native
compilation step.


## H3 primer

H3 is Uber's hierarchical hexagonal grid system. It divides the globe into
hexagonal cells at 16 resolution levels (0 = very large, 15 = very small).

    res-5   average cell area  ~252 km²  (~16 km across)
    res-6   average cell area   ~36 km²  (~6 km across)
    res-7   average cell area    ~5 km²  (~2 km across)

Hexagons have a useful property over squares: every neighbour is equidistant
from the centre, which avoids the diagonal vs axial bias of a square grid.

H3 uses an aperture-7 hierarchy (each cell has ~7 children at the next finer
resolution). This means the parent of a res-7 cell at resolution 6 is NOT
simply obtained by shifting bits — `h3.cell_to_parent(cell, 6)` must be
called explicitly.

Each H3 cell ID is a **64-bit integer** encoding the resolution and the path
through the hierarchy from the base icosahedron face down to the cell.


## Why compress H3 IDs to 32 bits

Raw H3 uint64 IDs in a sorted array would require:
- BigInt comparisons in JavaScript (slow, ~3× overhead vs Number)
- 8 bytes per key, meaning only 7 records fit in a 64-byte cache line
  (with a 2-byte admin_id that's 10 bytes per record — 6.4 records per line)

By extracting only the unique bits of the cell ID, we get a uint32 that still
uniquely identifies the cell within its resolution:

    H3 res-6 cell ID   (64 bits, hexadecimal)
    8671e4b52ffffff

    in binary, the resolution and cell path occupy specific bit positions.
    Bits 27–51 identify a res-6 cell uniquely (25 bits):

    enc6 = (h3_int >> 27) & 0x1FFFFFF    →  fits in uint25, stored as uint32

    H3 res-7 cell ID similarly:

    enc7 = (h3_int >> 24) & 0xFFFFFFF    →  fits in uint28, stored as uint32

Both fit in 32 bits. The 6-byte record `(uint32 cell_id + uint16 admin_id)`
then allows 10 records per 64-byte cache line (60 bytes of records, 4 bytes
padding).


## Cache-line alignment

Modern CPUs load memory in **64-byte cache lines**. When a program reads any
byte in a 64-byte block, the entire block is fetched into L1 cache. If your
records are 64 bytes each, one miss fetches exactly one record. If your
records are 6 bytes, one miss can fetch 10 records — a linear scan of 10
entries is free once the first byte is touched.

s2 exploits this deliberately: each block is exactly 64 bytes = 10 records.
Once the cache miss pays for fetching the block, all 10 comparisons in the
linear scan cost nothing extra.

    cache line (64 bytes)
    ============================================================
    [cell_id u32][admin u16]   record 0     6 bytes
    [cell_id u32][admin u16]   record 1     6 bytes
    [cell_id u32][admin u16]   record 2     6 bytes
    ...
    [cell_id u32][admin u16]   record 9     6 bytes
    [padding                ]               4 bytes
    ============================================================


## Two-level directory structure

With potentially hundreds of thousands of records, a flat binary search would
require ~17–18 comparisons for ~100 k records. The directory reduces this.

A separate **directory array** stores just the first cell key of every block.
Phase 1 binary-searches the directory (one uint32 read per comparison, all
likely hot in L2 cache) to find the right block. Phase 2 linearly scans the
single block.

    all blocks:   B0  B1  B2  ...  B_N
    directory:   [k0][k1][k2]...[k_N]   ← first key of each block

    binary search directory → block index b
    read block b from memory  (one cache miss)
    linear scan 10 records → found or miss


## Two tables: L10 (coarse) and L12 (fine)

The index is split into two tables at different H3 resolutions:

    L10 table   res-6 cells   covers interior cells (one cell = one region)
    L12 table   res-7 cells   covers boundary cells (finer, for accuracy)

Most land points sit in cells whose parent (res-6) is entirely inside one
region. Those hit L10 and the lookup is done. Only points near borders, where
the res-6 parent straddles multiple regions, fall through to L12.

    query (lat, lon)
         |
         +---→  enc6 = encode(h3 res-6 parent)
         |            binary search L10 directory
         |            linear scan block
         |            hit (63% of land queries)  →  return admin_id
         |            miss ↓
         |
         +---→  enc7 = encode(h3 res-7 cell)
                      binary search L12 directory
                      linear scan block
                      hit (37% of land queries)  →  return admin_id
                      miss  →  null (ocean)

The 63/37 split comes from the ratio of interior to boundary cells in the
index, which depends on the polygon complexity of the dataset.


## Why not one table at a single resolution

A single res-7 table would have ~9× more records than the L10+L12 combination
(every interior res-6 cell maps to ~7 res-7 cells). This is reflected in the
file size: s2_geo.bin is 55 MB because it covers boundary cells at res-7.

A single res-6 table would be smaller but less accurate near borders, where
a 6-km cell might contain points from two different regions.

The two-table approach is the standard spatial-index tradeoff: use a coarse
index for the common (interior) case and a fine index for the uncommon
(boundary) case.


## Binary format (RGEO0001)

    offset  field
    ------  -----
    0       magic "RGEO0001"  (8 bytes)
    8       version           u32  (= 1)
    12      l10_count         u32  total L10 records
    16      l12_count         u32  total L12 records
    20      l10_dir_off       u32  offset of L10 directory
    24      l12_dir_off       u32  offset of L12 directory
    28      admin_off         u32  offset of admin table
    32      name_off          u32  offset of name table
    36      reserved          28 bytes

    64      L10 blocks        ceil(l10_count/10) × 64-byte blocks
    l10_dir_off   L10 directory    l10_block_count × u32
    l10_dir_off + l10_block_count*4   L12 blocks
    l12_dir_off   L12 directory    l12_block_count × u32
    admin_off     admin table      n × 5 bytes  (u8 c_idx + u16 a1_idx + u16 a2_idx)
    name_off      name tables      zstd-compressed JSON


## Building

```sh
cd s2
pip install -r requirements.txt
python builder.py <gadm_adm2.geojson>
# produces s2_geo.bin (~55 MB)
```

The builder runs point-in-polygon tests to classify each H3 cell at res-6 and
res-7, then writes the sorted block arrays. Python with shapely is fast enough
for this; the build takes a few minutes.


## Performance

    query type          latency
    ----------------    ----------
    interior (L10 hit)  ~1–2 µs
    boundary (L12 hit)  ~1.5–3 µs
    ocean               ~2 µs       (both tables searched, both miss)

    index file   55 MB

The higher latency vs z0 (~5× slower on interior queries) has two causes:
1. H3 cell computation (`h3.latLngToCell` + `h3.cellToParent`) happens in
   h3-js WASM, which has call overhead beyond what pure JS arithmetic needs.
2. z0's layer 0 is a direct array index — O(1) with no branching. s2's L10
   lookup is O(log N) binary search even for interior cells.
