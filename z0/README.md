# z0 — Morton-coded offline reverse geocoder

Binary format: **RGEO0002**
Builder: Zig (`builder.zig`)
UI: `ui/` (port 5174)

## How it works

Lookup is a two-layer process designed so the common case (a point well inside
a region) never touches the boundary table.

### Layer 0 — coarse 0.25° grid

The world is divided into a 1440 × 720 grid of 0.25°-square cells. For each
land cell the builder runs a point-in-polygon test and records the winning
`admin_id` (0–65 533).

At query time:
1. Map (lat, lon) to a grid cell index.
2. Check a bitmap — if the bit is 0 the cell is ocean; return `null`.
3. Read the `values` array at the popcount-ranked position for that cell.
4. If the value is a plain admin_id (≤ 0xFFFD), return it immediately.
   ~97% of land queries end here.

### Layer 1 — Morton boundary table

Cells whose grid square straddles a border are marked `SENTINEL_BOUNDARY`
(0xFFFF) in the values array. For these the query continues:

1. Compute the Morton code of (lat, lon) quantised to 12 bits per axis
   (4096 steps, giving ~44 km resolution at the equator).
2. Binary-search a directory of block first-keys to find the candidate
   64-byte block (10 records of 6 bytes: `uint32 morton + uint16 admin_id`).
3. Linear-scan within the block for an exact match.
4. If no match, retry with `morton >> 2` (the parent cell at half resolution).

### Quad-tree compression & collision guard

During the build, runs of four sibling Morton cells that all map to the same
admin are compressed into a single parent record (`child >> 2`). A binary
search guards against compressing into a Morton code that already exists as a
direct PIP result in a geographically unrelated region — the case that
previously caused Central Asian cells to collide with South American ones.

### Binary format (RGEO0002)

```
[0:8]    magic "RGEO0002"
[8:64]   header — uint32 offsets for each section
[…]      bitmap        (1440×720 bits, 1 = land)
[…]      rank table    (uint32 per 512-cell block, for fast popcount)
[…]      values array  (uint16 per land cell)
[…]      Morton blocks (64-byte blocks, sorted by first Morton key)
[…]      Morton dir    (uint32 first-key per block)
[…]      admin table   (5 bytes: u8 country_idx + u16 adm1_idx + u16 adm2_idx)
[…]      name tables   (zstd JSON {countries, adm1s, adm2s})
```

### Building

```sh
cd z0
python prepare.py <gadm_adm2.geojson> z0_prep.bin
zig build -Drelease=true
./zig-out/bin/builder z0_prep.bin z0_geo.bin
```

### Performance

- Interior query (layer 0 hit): ~200–400 ns
- Boundary query (layer 1 hit): ~1–2 µs
- File size for GADM ADM2 (47 205 regions): ~11 MB
