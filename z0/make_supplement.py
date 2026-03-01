"""
make_supplement.py — create a GeoJSON supplement for countries absent from GADM ADM2.

Writes supplement.geojson with one feature per country.
Properties use the GADM-style keys expected by prepare.py.
"""

import json
import sys

# ── Country polygon data ───────────────────────────────────────────────────
# Coordinates are [lon, lat] as required by GeoJSON.
# Polygons are simplified but accurate enough for 12-bit Morton resolution.

FEATURES = [
    # ── Singapore ──────────────────────────────────────────────────────────
    # Main island + southern islands.  Central point: 1.352°N, 103.820°E
    {
        "type": "Feature",
        "properties": {
            "GID_0": "SGP", "NAME_1": "Singapore", "NAME_2": "Singapore"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [103.637, 1.159], [103.988, 1.159],
                [104.088, 1.280], [104.088, 1.482],
                [103.800, 1.470], [103.636, 1.440],
                [103.600, 1.340], [103.637, 1.159],
            ]]
        }
    },

    # ── Mauritius (main island) ─────────────────────────────────────────────
    # Port Louis: -20.165°N, 57.490°E
    {
        "type": "Feature",
        "properties": {
            "GID_0": "MUS", "NAME_1": "Port Louis", "NAME_2": "Port Louis"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [57.295, -20.523], [57.795, -20.523],
                [57.823, -19.978], [57.295, -19.978],
                [57.295, -20.523],
            ]]
        }
    },

    # ── Cape Verde — Santiago island ────────────────────────────────────────
    # Praia: 14.933°N, -23.513°E
    {
        "type": "Feature",
        "properties": {
            "GID_0": "CPV", "NAME_1": "Santiago", "NAME_2": "Praia"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-23.752, 14.785], [-23.355, 14.785],
                [-23.355, 15.320], [-23.752, 15.320],
                [-23.752, 14.785],
            ]]
        }
    },
    # ── Cape Verde — São Vicente island (Mindelo) ───────────────────────────
    {
        "type": "Feature",
        "properties": {
            "GID_0": "CPV", "NAME_1": "São Vicente", "NAME_2": "Mindelo"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-25.077, 16.744], [-24.939, 16.744],
                [-24.939, 16.862], [-25.077, 16.862],
                [-25.077, 16.744],
            ]]
        }
    },
    # ── Cape Verde — Sal island ─────────────────────────────────────────────
    {
        "type": "Feature",
        "properties": {
            "GID_0": "CPV", "NAME_1": "Sal", "NAME_2": "Sal"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-22.978, 16.600], [-22.830, 16.600],
                [-22.830, 16.868], [-22.978, 16.868],
                [-22.978, 16.600],
            ]]
        }
    },

    # ── Greenland — Kommuneqarfik Sermersooq (covers Nuuk area) ─────────────
    # Nuuk: 64.184°N, -51.721°E
    # The municipality is huge but we need to cover Nuuk peninsula in western GRL.
    {
        "type": "Feature",
        "properties": {
            "GID_0": "GRL", "NAME_1": "Kommuneqarfik Sermersooq",
            "NAME_2": "Nuuk"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-54.0, 63.5], [-50.0, 63.5],
                [-50.0, 65.5], [-54.0, 65.5],
                [-54.0, 63.5],
            ]]
        }
    },
    # ── Greenland — Qeqqata (covers Sisimiut, central west coast) ──────────
    {
        "type": "Feature",
        "properties": {
            "GID_0": "GRL", "NAME_1": "Qeqqata", "NAME_2": "Sisimiut"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-55.0, 65.5], [-50.0, 65.5],
                [-50.0, 68.0], [-55.0, 68.0],
                [-55.0, 65.5],
            ]]
        }
    },
    # ── Greenland — Qaasuitsup (covers Ilulissat, NW Greenland) ─────────────
    {
        "type": "Feature",
        "properties": {
            "GID_0": "GRL", "NAME_1": "Qaasuitsup", "NAME_2": "Ilulissat"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-56.0, 68.0], [-49.0, 68.0],
                [-49.0, 73.0], [-56.0, 73.0],
                [-56.0, 68.0],
            ]]
        }
    },
    # ── Greenland — Kujalleq (southern Greenland) ────────────────────────────
    {
        "type": "Feature",
        "properties": {
            "GID_0": "GRL", "NAME_1": "Kujalleq", "NAME_2": "Qaqortoq"
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-48.5, 59.5], [-42.0, 59.5],
                [-42.0, 63.5], [-48.5, 63.5],
                [-48.5, 59.5],
            ]]
        }
    },
]

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "supplement.geojson"
    fc = {"type": "FeatureCollection", "features": FEATURES}
    with open(out, "w") as f:
        json.dump(fc, f)
    print(f"Wrote {len(FEATURES)} features to {out}")

if __name__ == "__main__":
    main()
