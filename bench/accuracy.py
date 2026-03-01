#!/usr/bin/env python3
"""
accuracy.py — Compare reverse geocoders against Nominatim ground truth.

For each city in the list, queries Nominatim (OpenStreetMap) for the
authoritative ISO-3166-1 alpha-2 country code, then runs z0, s2, and h3
geocoders and reports accuracy.

Usage:
    cd bench
    # First run: fetch ground truth from Nominatim (1 req/s rate limit)
    python accuracy.py --fetch
    # Subsequent runs reuse the cache
    python accuracy.py
    # Skip geocoders you haven't built
    python accuracy.py --skip-s2 --skip-h3
    # Use custom binary paths
    python accuracy.py --z0 ../z0/z0_geo_gadm_full.bin
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.request
from typing import Optional

# ---------------------------------------------------------------------------
# ~60 major world cities across all continents + edge/ocean cases
# ---------------------------------------------------------------------------

CITIES = [
    # name                              lat         lon
    # -- Africa --
    ("Cairo, Egypt",               30.0444,   31.2357),
    ("Lagos, Nigeria",              6.5244,    3.3792),
    ("Nairobi, Kenya",             -1.2921,   36.8219),
    ("Johannesburg, ZA",          -26.2041,   28.0473),
    ("Addis Ababa, Ethiopia",       9.0320,   38.7423),
    ("Accra, Ghana",                5.6037,   -0.1870),
    ("Casablanca, Morocco",        33.5731,   -7.5898),
    ("Dar es Salaam, Tanzania",    -6.7924,   39.2083),
    ("Kinshasa, DR Congo",         -4.4419,   15.2663),
    ("Abidjan, Ivory Coast",        5.3600,   -4.0083),
    # -- Asia --
    ("Tokyo, Japan",               35.6762,  139.6503),
    ("Beijing, China",             39.9042,  116.4074),
    ("Mumbai, India",              19.0760,   72.8777),
    ("Delhi, India",               28.6139,   77.2090),
    ("Shanghai, China",            31.2304,  121.4737),
    ("Seoul, South Korea",         37.5665,  126.9780),
    ("Jakarta, Indonesia",         -6.2088,  106.8456),
    ("Bangkok, Thailand",          13.7563,  100.5018),
    ("Karachi, Pakistan",          24.8607,   67.0011),
    ("Istanbul, Turkey",           41.0082,   28.9784),
    ("Tehran, Iran",               35.6892,   51.3890),
    ("Baghdad, Iraq",              33.3152,   44.3661),
    ("Dhaka, Bangladesh",          23.8103,   90.4125),
    ("Ulaanbaatar, Mongolia",      47.8864,  106.9057),
    ("Singapore",                   1.3521,  103.8198),
    # -- Europe --
    ("London, UK",                 51.5074,   -0.1278),
    ("Paris, France",              48.8566,    2.3522),
    ("Berlin, Germany",            52.5200,   13.4050),
    ("Rome, Italy",                41.9028,   12.4964),
    ("Madrid, Spain",              40.4168,   -3.7038),
    ("Warsaw, Poland",             52.2297,   21.0122),
    ("Kyiv, Ukraine",              50.4501,   30.5234),
    ("Moscow, Russia",             55.7558,   37.6173),
    ("Stockholm, Sweden",          59.3293,   18.0686),
    ("Athens, Greece",             37.9838,   23.7275),
    ("Kaliningrad, Russia",        54.7065,   20.5109),
    ("Reykjavik, Iceland",         64.1466,  -21.9426),
    # -- Americas --
    ("New York, USA",              40.7128,  -74.0060),
    ("Los Angeles, USA",           34.0522, -118.2437),
    ("São Paulo, Brazil",         -23.5505,  -46.6333),
    ("Mexico City, Mexico",        19.4326,  -99.1332),
    ("Buenos Aires, Argentina",   -34.6037,  -58.3816),
    ("Bogotá, Colombia",            4.7110,  -74.0721),
    ("Lima, Peru",                -12.0464,  -77.0428),
    ("Santiago, Chile",           -33.4489,  -70.6693),
    ("Havana, Cuba",               23.1136,  -82.3666),
    ("Toronto, Canada",            43.6532,  -79.3832),
    ("Vancouver, Canada",          49.2827, -123.1207),
    # -- Oceania --
    ("Sydney, Australia",         -33.8688,  151.2093),
    ("Auckland, New Zealand",     -36.8485,  174.7633),
    ("Port Moresby, Papua NG",     -9.4438,  147.1803),
    # -- Islands / remote land --
    ("Nuuk, Greenland",            64.1836,  -51.7214),
    ("Port Louis, Mauritius",     -20.1654,   57.4896),
    ("Praia, Cape Verde",          14.9331,  -23.5133),
    # -- Coastal / edge cases --
    ("Cape Town, South Africa",   -33.9249,   18.4241),
    ("Tripoint DE/FR/CH",          47.5897,    7.5897),
    # -- Ocean --
    ("Mid-Atlantic (ocean)",        0.0,     -30.0),
    ("South Pacific (ocean)",     -40.0,    -140.0),
    ("Arctic (ocean)",             85.0,       0.0),
]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT    = "reverse-geocode-accuracy-bench/1.0 (github.com/Lulzx/reverse-geocode)"
CACHE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nominatim_cache.json")

# ISO 3166-1 alpha-3 → alpha-2 (covers all countries returned by z0/s2 geocoders)
_ISO3_TO_ISO2 = {
    "ABW":"AW","AFG":"AF","AGO":"AO","AIA":"AI","ALA":"AX","ALB":"AL","AND":"AD",
    "ANT":"AN","ARE":"AE","ARG":"AR","ARM":"AM","ASM":"AS","ATG":"AG","AUS":"AU",
    "AUT":"AT","AZE":"AZ","BDI":"BI","BEL":"BE","BEN":"BJ","BFA":"BF","BGD":"BD",
    "BGR":"BG","BHR":"BH","BHS":"BS","BIH":"BA","BLM":"BL","BLR":"BY","BLZ":"BZ",
    "BMU":"BM","BOL":"BO","BRA":"BR","BRB":"BB","BRN":"BN","BTN":"BT","BVT":"BV",
    "BWA":"BW","CAF":"CF","CAN":"CA","CCK":"CC","CHE":"CH","CHL":"CL","CHN":"CN",
    "CIV":"CI","CMR":"CM","COD":"CD","COG":"CG","COK":"CK","COL":"CO","COM":"KM",
    "CPV":"CV","CRI":"CR","CUB":"CU","CXR":"CX","CYM":"KY","CYP":"CY","CZE":"CZ",
    "DEU":"DE","DJI":"DJ","DMA":"DM","DNK":"DK","DOM":"DO","DZA":"DZ","ECU":"EC",
    "EGY":"EG","ERI":"ER","ESP":"ES","EST":"EE","ETH":"ET","FIN":"FI","FJI":"FJ",
    "FLK":"FK","FRA":"FR","FRO":"FO","FSM":"FM","GAB":"GA","GBR":"GB","GEO":"GE",
    "GGY":"GG","GHA":"GH","GIB":"GI","GIN":"GN","GLP":"GP","GMB":"GM","GNB":"GW",
    "GNQ":"GQ","GRC":"GR","GRD":"GD","GRL":"GL","GTM":"GT","GUF":"GF","GUM":"GU",
    "GUY":"GY","HKG":"HK","HMD":"HM","HND":"HN","HRV":"HR","HTI":"HT","HUN":"HU",
    "IDN":"ID","IMN":"IM","IND":"IN","IOT":"IO","IRL":"IE","IRN":"IR","IRQ":"IQ",
    "ISL":"IS","ISR":"IL","ITA":"IT","JAM":"JM","JEY":"JE","JOR":"JO","JPN":"JP",
    "KAZ":"KZ","KEN":"KE","KGZ":"KG","KHM":"KH","KIR":"KI","KNA":"KN","KOR":"KR",
    "KWT":"KW","LAO":"LA","LBN":"LB","LBR":"LR","LBY":"LY","LCA":"LC","LIE":"LI",
    "LKA":"LK","LSO":"LS","LTU":"LT","LUX":"LU","LVA":"LV","MAC":"MO","MAF":"MF",
    "MAR":"MA","MCO":"MC","MDA":"MD","MDG":"MG","MDV":"MV","MEX":"MX","MHL":"MH",
    "MKD":"MK","MLI":"ML","MLT":"MT","MMR":"MM","MNE":"ME","MNG":"MN","MNP":"MP",
    "MOZ":"MZ","MRT":"MR","MSR":"MS","MTQ":"MQ","MUS":"MU","MWI":"MW","MYS":"MY",
    "MYT":"YT","NAM":"NA","NCL":"NC","NER":"NE","NFK":"NF","NGA":"NG","NIC":"NI",
    "NIU":"NU","NLD":"NL","NOR":"NO","NPL":"NP","NRU":"NR","NZL":"NZ","OMN":"OM",
    "PAK":"PK","PAN":"PA","PCN":"PN","PER":"PE","PHL":"PH","PLW":"PW","PNG":"PG",
    "POL":"PL","PRI":"PR","PRK":"KP","PRT":"PT","PRY":"PY","PSE":"PS","PYF":"PF",
    "QAT":"QA","REU":"RE","ROU":"RO","RUS":"RU","RWA":"RW","SAU":"SA","SDN":"SD",
    "SEN":"SN","SGP":"SG","SGS":"GS","SHN":"SH","SJM":"SJ","SLB":"SB","SLE":"SL",
    "SLV":"SV","SMR":"SM","SOM":"SO","SPM":"PM","SRB":"RS","SSD":"SS","STP":"ST",
    "SUR":"SR","SVK":"SK","SVN":"SI","SWE":"SE","SWZ":"SZ","SYC":"SC","SYR":"SY",
    "TCA":"TC","TCD":"TD","TGO":"TG","THA":"TH","TJK":"TJ","TKL":"TK","TKM":"TM",
    "TLS":"TL","TON":"TO","TTO":"TT","TUN":"TN","TUR":"TR","TUV":"TV","TWN":"TW",
    "TZA":"TZ","UGA":"UG","UKR":"UA","UMI":"UM","URY":"UY","USA":"US","UZB":"UZ",
    "VAT":"VA","VCT":"VC","VEN":"VE","VGB":"VG","VIR":"VI","VNM":"VN","VUT":"VU",
    "WLF":"WF","WSM":"WS","YEM":"YE","ZAF":"ZA","ZMB":"ZM","ZWE":"ZW",
    # Territories / special codes
    "GUF":"GF","GLP":"GP","MTQ":"MQ","REU":"RE","MYT":"YT",
    "ESH":"EH","SJM":"SJ","BVT":"BV","ATF":"TF","HMD":"HM",
    "ALA":"AX","CCK":"CC","CXR":"CX","NFK":"NF","UMI":"UM",
    "TWN":"TW","HKG":"HK","MAC":"MO","PSE":"PS","XKX":"XK",
    "SRB":"RS","MNE":"ME","BIH":"BA","MKD":"MK","GEO":"GE",
    "ARM":"AM","AZE":"AZ",
}


def iso3_to_iso2(code: Optional[str]) -> Optional[str]:
    """Convert ISO-3166-1 alpha-3 to alpha-2. Returns original value if no match."""
    if code is None or len(code) != 3:
        return code
    return _ISO3_TO_ISO2.get(code.upper(), code)


# ---------------------------------------------------------------------------
# Nominatim helpers
# ---------------------------------------------------------------------------

def fetch_nominatim(lat: float, lon: float) -> Optional[str]:
    """Query Nominatim reverse geocoding. Returns uppercase ISO2 or None for ocean."""
    url = f"{NOMINATIM_URL}?lat={lat}&lon={lon}&format=json&zoom=3"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        cc = data.get("address", {}).get("country_code")
        return cc.upper() if cc else None
    except Exception as exc:
        print(f"  Nominatim error ({lat},{lon}): {exc}", file=sys.stderr)
        return None


def build_cache() -> dict:
    """Fetch Nominatim ground truth for all cities, respecting 1 req/s."""
    cache = {}
    print(f"Fetching {len(CITIES)} Nominatim lookups (1 req/s) …")
    for i, (name, lat, lon) in enumerate(CITIES, 1):
        result = fetch_nominatim(lat, lon)
        cache[name] = result
        symbol = result if result else "ocean"
        print(f"  [{i:3}/{len(CITIES)}] {name:<38} → {symbol}")
        time.sleep(1.1)   # Nominatim policy: max 1 req/s
    return cache


def load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        sys.exit(
            f"Cache not found: {CACHE_FILE}\n"
            "Run with --fetch first to download Nominatim ground truth."
        )
    with open(CACHE_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Geocoder loading (importlib isolation — same technique as bench.py)
# ---------------------------------------------------------------------------

def _load_geocoder(module_dir: str):
    spec = importlib.util.spec_from_file_location(
        f"query_{os.path.basename(module_dir)}",
        os.path.join(module_dir, "query.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, module_dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod.ReverseGeocoder


def _country(result: Optional[dict]) -> Optional[str]:
    """Extract country field from geocoder result, normalised to ISO-2."""
    if result is None:
        return None
    raw = result.get("country") or result.get("country_code")
    if raw is None:
        return None
    raw = raw.strip()
    if raw in ("UNK", ""):
        return None          # h3 builder bug — treat as no result
    # z0 / s2 store ISO-3; Nominatim uses ISO-2. Convert so comparisons work.
    return iso3_to_iso2(raw) or raw


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def _match(geocoder_val: Optional[str], nominatim_val: Optional[str]) -> bool:
    """Both None (ocean) is a match. Compare case-insensitively otherwise."""
    if nominatim_val is None and geocoder_val is None:
        return True
    if nominatim_val is None or geocoder_val is None:
        return False
    return geocoder_val.strip().upper() == nominatim_val.strip().upper()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Accuracy benchmark for reverse geocoders against Nominatim."
    )
    parser.add_argument("--fetch",    action="store_true",
                        help="Download Nominatim ground truth and save cache.")
    parser.add_argument("--z0",  default="../z0/z0_geo_gadm_full.bin")
    parser.add_argument("--s2",  default="../s2/s2_geo.bin")
    parser.add_argument("--h3",  default="../h3/h3_geo.bin")
    parser.add_argument("--skip-z0", action="store_true")
    parser.add_argument("--skip-s2", action="store_true")
    parser.add_argument("--skip-h3", action="store_true")
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # ---- ground truth -------------------------------------------------------
    if args.fetch:
        cache = build_cache()
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        print(f"\nCached to {CACHE_FILE}")
    else:
        cache = load_cache()

    # ---- load geocoders -----------------------------------------------------
    geocoders = {}

    if not args.skip_z0 and os.path.exists(args.z0):
        try:
            RG = _load_geocoder(os.path.join("..", "z0"))
            geocoders["z0"] = RG(args.z0)
        except Exception as exc:
            print(f"z0 load failed: {exc}", file=sys.stderr)

    if not args.skip_s2 and os.path.exists(args.s2):
        try:
            RG = _load_geocoder(os.path.join("..", "s2"))
            geocoders["s2"] = RG(args.s2)
        except Exception as exc:
            print(f"s2 load failed: {exc}", file=sys.stderr)

    if not args.skip_h3 and os.path.exists(args.h3):
        try:
            RG = _load_geocoder(os.path.join("..", "h3"))
            geocoders["h3"] = RG(args.h3)
        except Exception as exc:
            print(f"h3 load failed: {exc}", file=sys.stderr)

    if not geocoders:
        print("No geocoders loaded — check binary paths or use --skip-* flags.")
        return

    labels = list(geocoders.keys())

    # ---- run and compare ----------------------------------------------------
    # Column widths
    W_NAME = 38
    W_CC   = 7

    header = f"{'city':<{W_NAME}} {'nominatim':>{W_CC}}"
    for lbl in labels:
        header += f"  {lbl:>{W_CC}}"
    print()
    print(header)
    print("-" * len(header))

    totals   = {lbl: 0 for lbl in labels}
    hits     = {lbl: 0 for lbl in labels}
    misses   = {lbl: [] for lbl in labels}

    for name, lat, lon in CITIES:
        truth = cache.get(name)
        truth_str = truth if truth else "ocean"

        row = f"{name:<{W_NAME}} {truth_str:>{W_CC}}"

        for lbl in labels:
            result     = geocoders[lbl].lookup(lat, lon)
            gc_country = _country(result)

            # ocean match: geocoder returns None, nominatim returns None
            ok = _match(gc_country, truth)

            if truth is not None:          # only count land points for accuracy
                totals[lbl] += 1
                if ok:
                    hits[lbl] += 1
                else:
                    # record raw country value for the miss list
                    raw_cc = (result or {}).get("country") or (result or {}).get("country_code")
                    miss_got = raw_cc.strip() if raw_cc and raw_cc.strip() not in ("", ) else "ocean"
                    misses[lbl].append((name, truth, miss_got))

            display = gc_country if gc_country else ("ocean" if result is None else "UNK")
            flag    = "✓" if ok else "✗"
            row    += f"  {display:>{W_CC-2}}{flag} "

        print(row)

    # ---- summary ------------------------------------------------------------
    print("-" * len(header))
    summary_row = f"{'accuracy (land points)':<{W_NAME}} {' ':>{W_CC}}"
    for lbl in labels:
        n, h = totals[lbl], hits[lbl]
        pct  = 100.0 * h / n if n else 0.0
        summary_row += f"  {h}/{n} ({pct:.0f}%)"
    print(summary_row)

    # ---- per-geocoder miss list ---------------------------------------------
    for lbl in labels:
        if misses[lbl]:
            print(f"\n{lbl} misses:")
            for city, expected, got in misses[lbl]:
                got_str = got if got else "None/ocean"
                print(f"  {city:<{W_NAME}}  expected {expected}  got {got_str}")

    print()

    # ---- close geocoders ----------------------------------------------------
    for rg in geocoders.values():
        try:
            rg.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
