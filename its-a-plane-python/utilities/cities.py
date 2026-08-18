"""
cities.py — Nearest city lookup using GeoNames cities5000 dataset.

Downloads cities5000.zip from GeoNames on first run and caches as
cities.json in the project root. Subsequent lookups are instant.

Source: https://download.geonames.org/export/dump/cities5000.zip
No API key required. ~50K cities with population > 5000.

Usage:
    from utilities.cities import get_nearest_city
    nearest = get_nearest_city(40.6413, -73.7781)
    # {"name": "New York City", "distance_km": 18.3}
"""

import json
import math
import os
import threading
import zipfile
from io import BytesIO

import requests

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "cities.json")
ZIP_URL = "https://download.geonames.org/export/dump/cities5000.zip"

CACHE_VERSION = 1

# In-memory list: [[name, lat, lon], ...]
_db = []
_loaded = False
_load_lock = threading.Lock()


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two points."""
    lat1, lon1 = math.radians(lat1), math.radians(lon1)
    lat2, lon2 = math.radians(lat2), math.radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _download_and_build():
    """Download cities5000.zip, extract, and build city list."""
    print("[Cities] Downloading cities5000 database...")
    try:
        r = requests.get(ZIP_URL, timeout=(10, 60))
        r.raise_for_status()

        cities = []
        with zipfile.ZipFile(BytesIO(r.content)) as zf:
            with zf.open("cities5000.txt") as f:
                for line in f:
                    fields = line.decode("utf-8").split("\t")
                    if len(fields) < 6:
                        continue
                    name = fields[2] or fields[1]  # asciiname preferred (bitmap font safe)
                    try:
                        lat = float(fields[4])
                        lon = float(fields[5])
                    except (ValueError, IndexError):
                        continue
                    cities.append([name, lat, lon])

        cache_data = {"_version": CACHE_VERSION, "cities": cities}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f)
        print(f"[Cities] Database built — {len(cities)} cities cached to cities.json (v{CACHE_VERSION})")
        return cities

    except Exception as e:
        print(f"[Cities] Download failed: {e}")
        return []


def _load():
    """Load from cache file or download if not present. Thread-safe."""
    global _db, _loaded
    if _loaded:
        return

    with _load_lock:
        if _loaded:  # double-check after acquiring lock
            return

        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)

                if isinstance(raw, dict) and raw.get("_version") == CACHE_VERSION:
                    _db = raw.get("cities", [])
                    _loaded = True
                    return
                else:
                    version_found = raw.get("_version", "none") if isinstance(raw, dict) else "legacy"
                    print(f"[Cities] Cache version mismatch (found: {version_found}, need: {CACHE_VERSION}) — rebuilding")
            except Exception as e:
                print(f"[Cities] Cache load failed: {e} — re-downloading")

        _db = _download_and_build()
        _loaded = True


# Spatial index: cities bucketed by whole-degree (lat, lon). Built once, ~68k
# entries -> ~7.7k buckets. Lets a lookup examine a handful of buckets instead of
# every city.
_EARTH_R_KM = 6371.0
_KM_PER_DEG_LAT = math.radians(1.0) * _EARTH_R_KM     # ~111.19

_grid = None
_grid_src = None    # the exact _db list object the grid was built from


def _build_grid():
    """Bucket the loaded city list by integer degree. Idempotent.

    Keyed to the identity of _db, not merely "have we built one" — refresh() and
    the tests swap _db for a different list, and a grid left over from the previous
    list would silently answer from stale data.
    """
    global _grid, _grid_src
    if _grid is not None and _grid_src is _db:
        return _grid
    src = _db                       # snapshot: _db may be swapped mid-build, and
    g = {}                          # labelling a stale grid as fresh never heals
    for name, clat, clon in src:
        # Wrap the key exactly as lookups do, so a city at lon == +180.0 (legal,
        # though GeoNames normalises to [-180, 180)) lands in a bucket we search.
        g.setdefault((int(clat // 1), ((int(clon // 1) + 180) % 360) - 180),
                     []).append((name, clat, clon))
    _grid, _grid_src = g, src
    return _grid


def get_nearest_city(latitude, longitude):
    """
    Find the nearest city to the given coordinates.

    Returns {"name": str, "distance_km": float} or None if no cities loaded.

    PERFORMANCE: reachable from the RENDER THREAD (scenes/trackedstats.py ->
    landmarks.get_nearest_landmark -> here, whenever Nominatim has no settlement for
    the area, which is normal over sparse terrain). A haversine over all ~68,000
    cities measured 437ms on a Pi 3A+ against a 100ms frame budget — a tracked
    flight over eastern Washington froze the scroll on every requery.

    Searches the grid outward in rings, examining only each ring's PERIMETER, and
    stops once the nearest unexamined ring cannot hold anything closer than the best
    hit so far. Results are identical to an exhaustive scan.

    Two things here are easy to get wrong and are deliberate:

    * Perimeter, not the full square. Iterating (2r+1)^2 cells per ring and
      `continue`-ing over the interior is O(R^3) overall; at the poles rings grow to
      R=180 and that measured 482ms versus 29.6ms for the exhaustive scan it
      replaced — i.e. 16x SLOWER than doing nothing, and seconds on a Pi.
    * The stopping bound is a great-circle distance, not `ring * km_per_degree`.
      Degrees of longitude measured along a parallel OVERSTATE how far away the
      meridian actually is (a great circle cuts poleward), so the flat version is
      not a lower bound and returns wrong answers: with cities at (41.29, 0.999) and
      (60.999, 42.001), a query at (60.999, 0.999) stopped early and reported the
      2190km city instead of the 2174km one.
    """
    _load()
    if not _db:
        return None
    grid = _build_grid()

    ilat, ilon = int(latitude // 1), int(longitude // 1)
    coslat = math.cos(math.radians(latitude))

    def _min_dist_beyond(ring):
        """Lower bound on the distance to anything in an unexamined cell.

        After examining rings 0..ring, every unexamined cell is at least `ring`
        whole degrees away in latitude or longitude (the query may sit at the edge
        of its own cell, which is what makes it `ring` and not `ring + 1`).
        """
        by_lat = ring * _KM_PER_DEG_LAT
        # Distance from the point to the meridian `ring` degrees away — the
        # cross-track distance to that great circle. Saturates at 90 degrees.
        s = math.sin(math.radians(min(ring, 90))) * coslat
        by_lon = _EARTH_R_KM * math.asin(min(1.0, max(0.0, s)))
        return min(by_lat, by_lon)

    # Near the poles cos(lat) -> 0, so the meridian bound collapses and the ring
    # search cannot prune longitudes: uncapped it walks to ring 180 and ends up
    # SLOWER than the scan it replaced (measured 482ms vs 30ms before the perimeter
    # fix, 54ms after). Cap the rings and finish exhaustively, bounding the worst
    # case at ~1.3x one full scan instead of 16x.
    #
    # 45 is chosen, not arbitrary: a remote-Pacific query such as (45, -179.5) finds
    # its nearest city (Petropavlovsk-Kamchatsky) at ring 22, so a smaller cap threw
    # that case into the fallback. Above |lat| ~78 the bound cannot converge at all
    # and the fallback is taken by design -- polar tracked flights pay roughly what
    # the original code cost everywhere, which is the honest trade.
    _MAX_RING = 45

    best_name, best_dist = None, float("inf")
    ring = 0
    while ring <= _MAX_RING:
        if ring == 0:
            cells = [(0, 0)]
        elif 2 * ring + 1 >= 360:
            # The ring spans every longitude; walk the two latitude rows whole
            # rather than emitting 360+ duplicate wrapped columns.
            cells = [(d, dlon) for d in (-ring, ring) for dlon in range(-180, 180)]
        else:
            cells = [(d, dlon) for d in (-ring, ring)
                     for dlon in range(-ring, ring + 1)]                 # top+bottom
            cells += [(dlat, d) for d in (-ring, ring)
                      for dlat in range(-ring + 1, ring)]                # left+right
        for dlat, dlon in cells:
            clat_key = ilat + dlat
            if clat_key < -90 or clat_key > 90:
                continue            # no cities past the poles
            for name, clat, clon in grid.get(
                    (clat_key, ((ilon + dlon + 180) % 360) - 180), ()):
                d = _haversine_km(latitude, longitude, clat, clon)
                if d < best_dist:
                    best_dist, best_name = d, name
        if best_name is not None and best_dist <= _min_dist_beyond(ring):
            return {"name": best_name, "distance_km": best_dist}
        ring += 1

    # Bound not met within _MAX_RING (high latitude, or a very empty region):
    # finish exhaustively so the answer is always the true nearest.
    best_name, best_dist = None, float("inf")
    for name, clat, clon in _db:
        d = _haversine_km(latitude, longitude, clat, clon)
        if d < best_dist:
            best_dist, best_name = d, name

    if best_name is None:
        return None

    return {"name": best_name, "distance_km": best_dist}


def refresh():
    """Force re-download of cities database."""
    global _db, _loaded
    _loaded = False
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    _load()
