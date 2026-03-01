# z0 — Morton-coded offline reverse geocoder

Binary format: **RGEO0002**
Builder: **Zig** (`builder.zig`)
UI: `ui/` (port 5174)

z0 is the most heavily engineered of the three implementations. The goal was
to push lookup latency as low as possible in a browser JavaScript environment
while keeping the index file small enough to load instantly.


## The core insight: most queries are easy

The key observation driving the entire design is that ~97% of reverse geocode
queries are for points that are well inside a region — nowhere near a border.
For those queries, a coarse grid lookup is sufficient. Only the remaining ~3%
(points near borders) need finer-grained handling.

This splits the problem into two tiers:

    land surface of Earth
         |
         +--- 97% interior cells  →  coarse 0.25° grid  →  single array read
         |
         +--- 3% boundary cells   →  Morton table  →  binary search + linear scan


## Layer 0: the 0.25° grid

The entire globe is divided into a 1440 × 720 grid (one cell per 0.25° of
latitude and longitude, or roughly 28 km × 28 km at the equator).

    longitude  -180  -135  -90  -45   0   45   90  135  180
    latitude                                                      1440 cols
      +90   ....................................................
             ......CCCCCCCCCCCCCCCCCC.......RRRRRRRRRR..........
      +45   ...AAAAAAAAAAAAAAAAAAA.........RRRRRRRRRRRRRRR......
             ..AAAAAAAAAAAAAAAAAA.................RRRRRRRRRR....
       0   ..SSSSSS.....AAAAAAAAAAAAAA..........RRRRRRRRRRR....
             .SSSSSS..........AAAAAAAAA.........IIIIIIIIIIIII..
      -45   ..SSSSSSS.....AAAAAAAAAAA.........IIIIIIIIIIIII...
             ......................AAAAAAAAAA.................
      -90   ....................................................
                                                                  720 rows

Each grid cell is classified at build time by running a point-in-polygon test
against all 47 214 ADM2 polygons. The outcome for each cell is one of:

    0x0000–0xFFFD   admin_id  — cell is entirely inside one region
    0xFFFE          ocean     — no land polygon covers this cell
    0xFFFF          boundary  — cell straddles a border (multiple regions nearby)

The build uses a spatial bucket index (2° × 2° tiles) so each cell only tests
against polygons that could plausibly overlap it, not all 47 205.


### Storing the grid compactly: bitmap + rank

Most of the Earth's surface is ocean (~71%). Storing a uint16 for every cell
including ocean cells would waste space. Instead, z0 uses a **succinct** data
structure:

    bitmap (1 bit per cell, 1440×720 = 129 600 bits = ~16 KB)
       |
       v  1 = land cell,  0 = ocean cell
       |
    values array  (only land cells, 1 uint16 each)


To find the value for cell at index `i`, we need its position in the `values`
array — which is the number of land cells before index `i` in the bitmap, i.e.
the popcount of all bits 0..(i-1).

Computing popcount naively is O(n). Instead, a **rank table** stores a
running count every 512 cells. To find the rank at index `i`:

    block    = i / 512
    into     = i mod 512
    rank     = rank_table[block]           ← base count for this 512-block
             + popcount(bitmap[block*64 .. block*64 + into/8])   ← partial byte
             + popcount(bitmap[block*64 + into/8] & mask)        ← sub-byte

This reduces the work to scanning at most 64 bytes, with the rank table lookup
providing the starting count for free.

    rank_table
    [0]  [1]  [2]  [3]  ...
      \    \    \    \
       0   412  831  1249   ← running count of land cells
            |
            v
       sum + partial popcount within block
            |
            v  index into values[]


## Layer 1: the Morton boundary table

When a cell is marked `0xFFFF` (boundary), we need to know which specific
region a precise (lat, lon) falls in. For this, z0 uses a **Morton-coded
spatial index**.

### What a Morton code is

A Morton code (Z-order curve) interleaves the bits of the x and y coordinates:

    lat_quantised = 2362   =  0b100100111010
    lon_quantised = 1841   =  0b011100110001

    interleave bits:
    lat:  _1_0_0_1_0_0_1_1_1_0_1_0
    lon:  0_1_1_1_0_0_1_1_0_0_0_1_

    morton: 011011010000011110100100  =  0x35E1240  (example)

The critical property: points that are geographically close tend to have
similar Morton codes. This means sorting records by Morton code produces a
spatial index — nearby points in the index are nearby on the globe.

### Morton quantisation

Coordinates are quantised to **12 bits per axis** (4096 steps):

    lat_q = floor( (lat + 90)  / 180 * 4096 )    →  0..4095
    lon_q = floor( (lon + 180) / 360 * 4096 )    →  0..4095

12 bits was chosen after the 16-bit version (65 536 steps, ~3 km resolution)
turned out to be unnecessarily fine. At 12 bits, one step is ~44 km at the
equator, which is smaller than most ADM2 regions but coarse enough that the
Morton table stays compact. The JS runtime initially used 65 536 — a bug that
caused every boundary lookup to return null because no Morton codes matched.


### Block structure

Records in the Morton table are packed into **64-byte blocks** (10 records of
6 bytes each, with 4 bytes padding):

    one block  =  64 bytes  =  one CPU cache line
    --------------------------------------------------
    [morton_0: u32][admin_0: u16]    6 bytes
    [morton_1: u32][admin_1: u16]    6 bytes
    ...                              6 bytes  × 10
    [padding                    ]    4 bytes
    --------------------------------------------------

Records within a block are sorted by Morton code. A separate **directory**
array stores the first Morton code of every block, enabling a two-phase lookup:

    phase 1:  binary search the directory  →  O(log N/10) comparisons
    phase 2:  linear scan within one block →  ≤ 10 comparisons, 1 cache miss


### Quad-tree compression

If four sibling Morton codes (sharing the same top bits) all map to the same
admin region, they can be replaced by one parent record at `morton >> 2`:

    four children:   m*4, m*4+1, m*4+2, m*4+3  →  one parent: m
    (where m = child >> 2)

This reduces the record count by up to 4× in uniform interior regions, at the
cost of a second lookup pass when the fine search misses:

    1.  search for morton       →  found? return admin_id
    2.  search for morton >> 2  →  found? return admin_id
    3.  both miss               →  ocean


### The collision bug (São Paulo / Xinjiang)

This compression produced a subtle bug. The parent Morton code `m = child >> 2`
is mathematically `interleaveBits(lat_q >> 1, lon_q >> 1)`. This represents
a cell at half the quantisation — but it is **not geographically nearby** the
children. The parent exists in a completely different region of the globe.

Specifically: Central Asian cells near Xinjiang, China had `lat_q ≈ 3024`,
`lon_q ≈ 3034`. Their parent codes at `lat_q = 1512`, `lon_q = 1517`
fell exactly on São Paulo, Brazil's Morton codes. After compression, 36 945
duplicate Morton codes appeared across geographically unrelated countries:

    BRA / RUS:  10 572 duplicates
    BRA / KAZ:   3 207 duplicates
    BRA / CHN:   1 896 duplicates
    ...

The fix: before compressing four children to parent `P`, binary-search the
already-committed records to check whether `P` already exists as a direct PIP
result. If it does, skip compression and keep all four children individually.


## Why Zig

The builder is written in Zig for three reasons:

1. **Performance.** The point-in-polygon pass over 475 157 polygon parts
   and 1 036 800 grid cells is CPU-intensive. Zig produces native code with
   explicit memory layout control and no GC pauses. The full build takes ~16 s.

2. **Explicitness.** Zig makes memory layout and alignment explicit. When
   writing a binary format that will be read by a DataView in JavaScript,
   having the compiler enforce struct sizes and alignment is valuable.

3. **WASM potential.** Zig can compile to WebAssembly. The same builder logic
   could eventually run in the browser directly, allowing users to build their
   own index from custom polygon data.


## Binary format (RGEO0002)

    offset  field
    ------  -----
    0       magic "RGEO0002"   (8 bytes)
    8       version            u32
    12      timestamp          u32
    16      bitmapOff          u32  → bitmap section offset
    20      rankOff            u32  → rank table offset
    24      valuesOff          u32  → values array offset
    28      landCellCount      u32  → length of values array
    32      mBlockOff          u32  → Morton block array offset
    36      mDirOff            u32  → Morton directory offset
    40      mRecCount          u32  → total Morton records
    44      mBlockCount        u32  → number of 64-byte blocks
    48      adminOff           u32  → admin table offset
    52      nameOff            u32  → name table offset (zstd JSON)
    56      reserved           8 bytes

    then: bitmap, rank table, values, Morton blocks, Morton dir,
          admin table (6 bytes/admin: u16 country_idx, u16 adm1_idx, u16 adm2_idx),
    name tables (zstd JSON)


## Building

```sh
cd z0
# Optionally create a supplement for countries missing from GADM:
python make_supplement.py supplement.geojson

python prepare.py <gadm_adm2.geojson> z0_prep.bin [--supplement supplement.geojson]
zig build -Drelease=true
./zig-out/bin/builder z0_prep.bin z0_geo.bin

# Build WASM module:
zig build wasm
# Output: zig-out/bin/geocoder.wasm (~1.5 KB)
```

`prepare.py` converts the GeoJSON into a compact binary polygon stream that
the Zig builder can process without a Python dependency at build time.
The optional `--supplement` argument appends extra features (e.g., for small
island nations absent from GADM) to improve coverage.


## Performance

    query type          latency
    ----------------    ----------
    interior (L0 hit)   ~200–400 ns
    boundary (L1 hit)   ~1–2 µs
    ocean               ~100–200 ns  (bitmap bit test, early exit)

    index file (47 214 regions)   11 MB
    prep binary (input to Zig)    1.1 GB  (raw polygon data, not committed)
