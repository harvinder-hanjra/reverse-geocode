# s2 — Block binary search geocoder (H3-backed)

Binary format: **RGEO0001**
Builder: Python (`builder.py`)
UI: `ui/` (port 5175)

## How it works

Despite the name, this implementation uses **H3** cells internally (the S2
geometry library is not a dependency). H3 cell IDs are encoded as compact
32-bit integers that fit neatly into a cache-line-aligned block structure.

### Cell encoding

H3 cell IDs are 64-bit integers. Two compact encodings are used:

| Level | H3 resolution | Encoding | Bits |
|---|---|---|---|
| L10 (coarse) | res-6 | `(h3_int >> 27) & 0x1FFFFFF` | 25 |
| L12 (fine)   | res-7 | `(h3_int >> 24) & 0xFFFFFFF` | 28 |

Both fit in a `uint32`, enabling a record size of 6 bytes
(`uint32 cell_id + uint16 admin_id`).

### Two-level block binary search

Records are grouped into **64-byte blocks** (10 records each, filling one CPU
cache line). A compact **directory** stores the first cell key of every block.

Lookup for a point (lat, lon):
1. Compute H3 res-7 cell → encode as `enc7`.
2. Compute its res-6 parent → encode as `enc6`.
3. Binary-search the L10 directory for the block whose first key ≤ `enc6`.
4. Linear-scan the 10-record block for an exact match.
   If found, look up the admin table → return result (~63% of land queries).
5. Repeat steps 3–4 against the L12 table using `enc7`.
   If found, return result (~37% of land queries).
6. Neither table matched → return `null` (ocean).

The two-level structure keeps the directory small enough to stay in L2 cache
and bounds each lookup to a single cache-line read for the block scan.

### Binary format (RGEO0001)

```
[0:8]    magic "RGEO0001"
[8:64]   header — version, record counts, section offsets
[64]     L10 block array  (ceil(l10_count/10) × 64-byte blocks)
[…]      L10 directory    (uint32 first-key per block)
[…]      L12 block array
[…]      L12 directory
[…]      admin table      (5 bytes: u8 country_idx + u16 adm1_idx + u16 adm2_idx)
[…]      name tables      (zstd JSON {countries, adm1, adm2})
```

### Building

```sh
cd s2
pip install -r requirements.txt
python builder.py <gadm_adm2.geojson>
# produces s2_geo.bin
```

### Performance

- Interior query (L10 hit): ~1–2 µs
- Boundary query (L12 hit): ~1.5–3 µs
- File size for GADM ADM2: ~55 MB (higher than z0 because res-7 covers
  boundary cells at finer granularity — ~9× more cells per region edge)
