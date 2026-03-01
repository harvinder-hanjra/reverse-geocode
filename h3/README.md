# h3 — H3 cell binary search geocoder

Binary format: **LKHA0001**
Builder: **Python** (`builder.py`)
UI: `ui/` (port 5176)

h3 is the simplest of the three implementations: a flat sorted array of
raw 64-bit H3 cell IDs, searched with a standard binary search. No spatial
tricks, no multi-level structures, no compression of keys. The goal was to
establish a baseline and understand how far plain binary search can get you.


## H3 cell coverage

At build time the builder fills H3 cells at two resolutions for each ADM2
polygon:

    for each polygon:
        fill res-5 cells  (~252 km² each, ~16 km across)
            these cover the interior — one cell, one region
        fill res-6 cells  (~36 km² each, ~6 km across)
            these cover boundary cells that straddle res-5 borders

The coarser res-5 covers most of the polygon area with fewer records. The
finer res-6 captures borders more accurately. This is similar in concept to
s2's L10/L12 split, but here both resolutions live in the same flat array —
there is no structural separation.

    polygon interior                       polygon border
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ |||||||||||||||||
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ |||||||||||||||||
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    filled with res-5 cells (large)        filled with res-6 cells (smaller)
    851 000 total records for geoBoundaries ADM2


## The record array

Every record is 12 bytes:

    h3_index   uint64  raw H3 cell ID (little-endian)
    packed_meta  uint32

The entire array is sorted by `h3_index`, allowing binary search.

    record 0     record 1     record 2     ...     record N
    [h3_id u64][meta u32]  [h3_id u64][meta u32]  ...
    |          |
    sorted ascending


## Lookup: three-resolution fallback

    given (lat, lon):

        cell6 = h3.latLngToCell(lat, lon, 6)         ← fine res, ~6 km cells
        search array for cell6
        found  →  decode meta  →  return {country, adm1, adm2}
             |
             miss
             |
        cell5 = h3.cellToParent(cell6, 5)             ← coarser, ~16 km cells
        search array for cell5
        found  →  decode meta  →  return {country, adm1, adm2}
             |
             miss
             |
        cell4 = h3.cellToParent(cell5, 4)             ← coarsest, ~107 km cells
        search array for cell4
        found  →  return {country, adm1, adm2}
             |
             miss  →  null (ocean or unmapped)

Why three levels? Small or narrow polygons might not contain any res-6 cell
centroid. A thin river border, a small island, or a very elongated district
could be missed entirely at res-6. The res-5 fallback catches most of these.
Res-4 is the last resort for genuinely tiny territories.

In practice the vast majority of lookups hit at res-6. The fallback levels are
rarely exercised but are critical for correctness on edge cases.


## The BigInt problem in JavaScript

H3 cell IDs are 64-bit unsigned integers. JavaScript's `Number` type is a
64-bit float (IEEE 754 double), which can only represent integers exactly up
to 2^53 = 9 007 199 254 740 992. H3 cell IDs regularly exceed this:

    example res-6 cell:  0x8671e4b52fffffff  =  9 604 866 430 115 635 199

    9.6 × 10^18  >>  9 × 10^15  =  safe integer limit

Storing this in a Number would silently corrupt the value. The browser
runtime (`ui/src/h3.js`) uses BigInt throughout:

    const lo32 = dv.getUint32(off,     true)   ← lower 32 bits
    const hi32 = dv.getUint32(off + 4, true)   ← upper 32 bits
    const key  = (BigInt(hi32) << 32n) | BigInt(lo32)

BigInt operations are inherently slower than Number operations in all JS
engines — roughly 3–5× slower for arithmetic and comparison. This is the
dominant cost in h3's lookup: not the binary search logic itself, but the
overhead of BigInt construction and comparison at every step.

s2 avoids this entirely by encoding H3 IDs as compact uint32 values. The
encoding discards the H3 structure bits and keeps only the spatially
meaningful bits, which fit in 25–28 bits — safely within Number's exact
integer range.


## The packed_meta encoding

Each record stores the names indirectly via a packed 32-bit integer:

    packed_meta  =  country_id[31:24]  |  state_offset[23:16]  |  district_offset[15:0]

    country_id      8 bits   →  max 256 countries
    state_offset    8 bits   →  max 256 ADM1 regions per country
    district_offset 16 bits  →  max 65 536 ADM2 regions per country

To decode:
    country  = names.countries[country_id]
    adm1     = names.adm1[country_id][state_offset]
    adm2     = names.adm2[country_id][district_offset]

The name table uses nested lists — one sublist per country — so the same
offset value means different things in different countries.


## What went wrong with the name table

The h3_geo.bin built from geoBoundaries data has only **1 country** ("UNK",
unknown), 1 ADM1 entry (empty string), and 39 564 ADM2 district names.

This happened because h3's builder looked for `shapeISO` as the country key,
but the actual geoBoundaries property name for the country code varies by
dataset version (`shapeGroup`, `ISO`, `GID_0`, etc.). When no known key
matched, the builder defaulted to "UNK" for every feature.

The result: all 851 000 records have `country_id = 0` ("UNK") and
`state_offset = 0` (""). Only the `district_offset` varies, giving 39 564
distinct district names — but no country or province hierarchy.

This is a data pipeline lesson: always print a sample of the resolved property
values before running a multi-hour build. If the first 10 features all show
"UNK" for country, stop and fix the key mapping.

The h3 UI works around this by falling back to the GeoJSON basemap for country
and ADM1 names, using only the district name from the geocoder result.


## Extracting names for the browser

The name table inside h3_geo.bin is zstd-compressed. Shipping a WASM
decompressor just to read ~20 KB of JSON was unappealing. Instead,
`extract_names.py` reads the binary once and writes `h3_names.json` as
plain JSON:

```sh
python extract_names.py h3_geo.bin h3_names.json
```

The UI loads both files in parallel. `h3.js` receives the pre-parsed names
object directly — no decompression at runtime, no extra dependency.


## Binary format (LKHA0001)

    offset  field
    ------  -----
    0       magic "LKHA0001"  (8 bytes)
    8       version           u32  (= 1)
    12      n_records         u32
    16      name_table_off    u32  offset of zstd-compressed name JSON
    20      record array      n_records × 12 bytes, sorted by h3_index
    name_table_off  zstd name JSON


## Building

```sh
cd h3
pip install -r requirements.txt
python builder.py <gadm_adm2.geojson>      # → h3_geo.bin
python extract_names.py                    # → h3_names.json (for browser)
```


## Performance

    query type          latency
    ----------------    ----------
    res-6 hit           ~3–5 µs
    res-5 fallback      ~5–8 µs  (two binary searches)
    res-4 fallback      ~7–12 µs (three binary searches)
    ocean               ~7 µs    (three misses)

    index file   10 MB   (851 502 records × 12 bytes)

The latency is dominated by BigInt overhead, not by the binary search itself.
A version using compact uint32 keys (like s2) would be ~3× faster. The flat
single-level structure also means no early-exit for interior cells — every
query pays the full binary search cost, unlike z0's O(1) grid lookup for
interior points.
