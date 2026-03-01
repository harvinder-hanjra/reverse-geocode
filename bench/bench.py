#!/usr/bin/env python3
"""
bench.py — Benchmark all three reverse geocoders on a shared city list.

Runs each geocoder N times per city and reports mean, median, p95, and p99
latency. Output is plain text suitable for pasting into results.md.

Usage:
    cd bench
    python bench.py [--n 10000] [--z0 ../z0/z0_geo.bin] \
                    [--s2 ../s2/s2_geo.bin] \
                    [--h3 ../h3/h3_geo.bin]
"""

import argparse
import os
import statistics
import sys
import time

# ---------------------------------------------------------------------------
# Test cities: interior, boundary, ocean, island, tiny territory
# ---------------------------------------------------------------------------

CITIES = [
    # name                      lat        lon        expected_country
    ("São Paulo, Brazil",      -23.5505,  -46.6333,  "BRA"),
    ("London, UK",              51.5074,   -0.1278,  "GBR"),
    ("Tokyo, Japan",            35.6762,  139.6503,  "JPN"),
    ("Nairobi, Kenya",          -1.2921,   36.8219,  "KEN"),
    ("Cairo, Egypt",            30.0444,   31.2357,  "EGY"),
    ("Sydney, Australia",      -33.8688,  151.2093,  "AUS"),
    ("Moscow, Russia",          55.7558,   37.6173,  "RUS"),
    ("Los Angeles, USA",        34.0522, -118.2437,  "USA"),
    ("Mumbai, India",           19.0760,   72.8777,  "IND"),
    ("Cape Town, South Africa", -33.9249,   18.4241,  "ZAF"),
    # boundary / tricky cases
    ("Tripoint DE/FR/CH",       47.5897,    7.5897,  None),
    ("Kaliningrad, Russia",     54.7065,   20.5109,  "RUS"),
    # ocean
    ("Mid-Atlantic",             0.0,     -30.0,     None),
    ("South Pacific",          -40.0,    -140.0,     None),
]

WARMUP = 100
N_DEFAULT = 5000


def fmt_us(ns_list):
    """Format a list of nanosecond measurements as µs stats string."""
    us = [v / 1000 for v in ns_list]
    return (
        f"mean={statistics.mean(us):.2f}µs  "
        f"median={statistics.median(us):.2f}µs  "
        f"p95={sorted(us)[int(len(us)*0.95)]:.2f}µs  "
        f"p99={sorted(us)[int(len(us)*0.99)]:.2f}µs"
    )


def _load_geocoder(module_dir):
    """Load query.ReverseGeocoder from a specific directory without polluting sys.modules."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"query_{os.path.basename(module_dir)}",
        os.path.join(module_dir, "query.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # Temporarily add the module's own directory to sys.path so its imports work
    sys.path.insert(0, module_dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod.ReverseGeocoder


def _run(rg, cities, n):
    results = {}
    for name, lat, lon, _ in cities:
        for _ in range(WARMUP):
            rg.lookup(lat, lon)
        times = []
        for _ in range(n):
            t0 = time.perf_counter_ns()
            rg.lookup(lat, lon)
            times.append(time.perf_counter_ns() - t0)
        results[name] = times
    return results


def bench_z0(bin_path, cities, n):
    RG = _load_geocoder(os.path.join(os.path.dirname(__file__), '..', 'z0'))
    return _run(RG(bin_path), cities, n)


def bench_s2(bin_path, cities, n):
    RG = _load_geocoder(os.path.join(os.path.dirname(__file__), '..', 's2'))
    return _run(RG(bin_path), cities, n)


def bench_h3(bin_path, cities, n):
    RG = _load_geocoder(os.path.join(os.path.dirname(__file__), '..', 'h3'))
    return _run(RG(bin_path), cities, n)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--n',  type=int, default=N_DEFAULT)
    parser.add_argument('--z0', default='../z0/z0_geo_gadm_full.bin')
    parser.add_argument('--s2', default='../s2/s2_geo.bin')
    parser.add_argument('--h3', default='../h3/h3_geo.bin')
    parser.add_argument('--skip-z0', action='store_true')
    parser.add_argument('--skip-s2', action='store_true')
    parser.add_argument('--skip-h3', action='store_true')
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print(f"Benchmarking with n={args.n} iterations per city, {WARMUP} warmup\n")

    geocoders = []
    if not args.skip_z0 and os.path.exists(args.z0):
        geocoders.append(('z0', args.z0, bench_z0))
    if not args.skip_s2 and os.path.exists(args.s2):
        geocoders.append(('s2', args.s2, bench_s2))
    if not args.skip_h3 and os.path.exists(args.h3):
        geocoders.append(('h3', args.h3, bench_h3))

    all_results = {}
    for label, path, fn in geocoders:
        print(f"--- {label} ({path}) ---")
        r = fn(path, CITIES, args.n)
        all_results[label] = r
        for name, lat, lon, expected in CITIES:
            print(f"  {name:<35}  {fmt_us(r[name])}")
        print()

    # Summary table: mean latency per geocoder across all cities
    if len(all_results) > 1:
        print("=== Summary (mean µs per city) ===")
        labels = list(all_results.keys())
        header = f"{'city':<35}" + "".join(f"  {l:>8}" for l in labels)
        print(header)
        print("-" * len(header))
        for name, lat, lon, _ in CITIES:
            row = f"{name:<35}"
            for l in labels:
                mean_us = statistics.mean(all_results[l][name]) / 1000
                row += f"  {mean_us:>7.2f}µ"
            print(row)


if __name__ == '__main__':
    main()
