# Reverse Geocode

Three offline reverse geocoders — given a (lat, lon), return the country and
administrative region — with interactive map UIs. No network required after
the data files are built.

| Implementation | Index file | Format | UI port |
|---|---|---|---|
| **z0** | `z0_geo.bin` | RGEO0002 | 5174 |
| **s2** | `s2_geo.bin` | RGEO0001 | 5175 |
| **h3** | `h3_geo.bin` | LKHA0001 | 5176 |

All three UIs share the same `adm2_render.geojson` basemap (GADM ADM2,
47 205 regions) and display lookup results + timing on hover.

## Structure

```
reverse-geocode/
├── z0/          Zig-built Morton-coded geocoder
│   ├── docs/    Binary format specification
│   └── ui/      Vite/MapLibre map (port 5174)
├── s2/          H3-backed block binary search geocoder
│   ├── docs/    Binary format specification
│   └── ui/      Vite/MapLibre map (port 5175)
├── h3/          H3 cell binary search geocoder
│   ├── docs/    Binary format specification
│   └── ui/      Vite/MapLibre map (port 5176)
├── extract_gadm.py         Pull ADM2 features from GADM SQLite
└── make_render_geojson.py  Build adm2_render.geojson from prep binary
```

## Data pipeline

```
GADM 4.1 SQLite
      │
      ▼ extract_gadm.py
gadm_adm2.geojson
      │
      ├─► z0/prepare.py  → z0_prep.bin  → z0/builder.zig → z0_geo.bin
      ├─► s2/builder.py                              → s2_geo.bin
      └─► h3/builder.py                              → h3_geo.bin

z0_prep.bin ─► make_render_geojson.py → adm2_render.geojson
```

## Running the UIs

```sh
cd z0/ui && bun dev   # http://localhost:5174
cd s2/ui && bun dev   # http://localhost:5175
cd h3/ui && bun dev   # http://localhost:5176
```

Each UI is a self-contained Vite project. The binary index and GeoJSON are
served from `public/data/` (symlinks to the files in the parent directory).

## Approach comparison

All three read an in-memory binary blob fetched once at page load and answer
every subsequent query in pure JavaScript with no network calls.

- **z0** uses a two-layer scheme: a coarse 0.25° grid covers ~97% of land
  queries in a single array lookup, and a Morton-sorted boundary table handles
  the rest. Lookup is typically < 1 µs.
- **s2** packs H3 cells into compact 32-bit keys and organises them into
  64-byte cache-line blocks for two-level binary search. Interior queries hit
  the coarse (res-6) table; boundary queries fall through to the fine (res-7)
  table.
- **h3** stores raw 64-bit H3 cell IDs in a flat sorted array and binary-searches
  with a three-resolution fallback (res 6 → 5 → 4). The 64-bit keys require
  BigInt arithmetic in JavaScript but the implementation is the simplest of the
  three.
