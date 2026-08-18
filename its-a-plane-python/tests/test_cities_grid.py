"""
test_cities_grid.py — the spatial index in utilities/cities.py.

get_nearest_city() is reachable from the RENDER THREAD (scenes/trackedstats.py ->
landmarks.get_nearest_landmark -> here, whenever Nominatim has no settlement for the
area, which is normal over sparse terrain). An exhaustive haversine over all ~68,000
cities measured 437ms on a Pi 3A+ against a 100ms frame budget, so a tracked flight
over eastern Washington froze the scroll on every requery.

The grid must therefore be BOTH fast and exactly equivalent to the old full scan.
These tests pin the equivalence — the speed is meaningless if the answer moves.

Mutation coverage, measured rather than assumed. These fail as they should:
  * flat (non-great-circle) stopping bound   -- the shipped wrong-answer bug
  * never widening past ring 0
  * dropping the grid staleness guard
  * unwrapped bucket keys (caught via work done, since the exhaustive fallback
    would otherwise rescue the answer and hide it)
  * reading _grid_src from the global instead of the snapshot

KNOWN GAP: a bound that breaks only slightly early (e.g. x1.02) is NOT caught. A
200,000-case adversarial search over sparse high-latitude configurations found no
input where that changes the answer, so the window is too narrow to pin with a
fixed case. It would produce a rarely-wrong city NAME, never a crash or a stall.
"""

import math
import random

import pytest

import utilities.cities as C


def _exhaustive(db, lat, lon):
    """The pre-grid implementation, kept here as the oracle."""
    best_name, best_dist = None, float("inf")
    for name, clat, clon in db:
        d = C._haversine_km(lat, lon, clat, clon)
        if d < best_dist:
            best_dist, best_name = d, name
    return (best_name, round(best_dist, 6)) if best_name else None


def _with_db(db):
    """Install a city list and clear the loaded/grid state around it."""
    C._db = db
    C._loaded = True
    C._grid = None
    C._grid_src = None


@pytest.fixture
def synthetic_db():
    old_db, old_loaded, old_grid, old_src = C._db, C._loaded, C._grid, C._grid_src
    random.seed(1234)
    db = [[f"city{i}", random.uniform(-89, 89), random.uniform(-180, 180)]
          for i in range(4000)]
    # deliberate clusters near the awkward places
    db += [["anti_a", 0.5, 179.6], ["anti_b", 0.5, -179.6],
           ["polar_n", 88.9, 20.0], ["polar_s", -88.9, -20.0]]
    _with_db(db)
    yield db
    C._db, C._loaded, C._grid, C._grid_src = old_db, old_loaded, old_grid, old_src


def test_matches_exhaustive_scan_everywhere(synthetic_db):
    """The whole point: identical answers to the old full scan."""
    random.seed(99)
    probes = [(47.68, -118.87),          # the position that caused the live freeze
              (0, 179.9), (0, -179.9),   # antimeridian, both sides
              (45, -179.5), (45, 179.5),
              (89.5, 0), (-89.5, 0),     # poles, where lon degrees collapse
              (0, 0)]
    probes += [(random.uniform(-89, 89), random.uniform(-180, 180)) for _ in range(200)]
    for lat, lon in probes:
        got = C.get_nearest_city(lat, lon)
        want = _exhaustive(synthetic_db, lat, lon)
        got_t = (got["name"], round(got["distance_km"], 6)) if got else None
        assert got_t == want, f"grid disagreed at {lat},{lon}: {got_t} != {want}"


def test_grid_rebuilds_when_db_is_replaced():
    """A grid left over from a previous _db would answer from stale data.

    This is not hypothetical: it broke test_cities_and_altitude when the grid was
    keyed only on 'have we built one yet'.
    """
    old = (C._db, C._loaded, C._grid, C._grid_src)
    try:
        _with_db([["alpha", 10.0, 10.0]])
        assert C.get_nearest_city(10.0, 10.01)["name"] == "alpha"
        # swap in a completely different world; the old grid must not survive
        C._db = [["beta", 10.0, 10.0]]
        C._loaded = True
        assert C.get_nearest_city(10.0, 10.01)["name"] == "beta"
    finally:
        C._db, C._loaded, C._grid, C._grid_src = old


def test_empty_db_returns_none():
    old = (C._db, C._loaded, C._grid, C._grid_src)
    try:
        _with_db([])
        assert C.get_nearest_city(40.0, -73.0) is None
    finally:
        C._db, C._loaded, C._grid, C._grid_src = old


def test_finds_a_far_city_when_the_local_cells_are_empty(synthetic_db):
    """Remote ocean: nothing for many degrees. The ring search must keep widening
    rather than give up (this is the trans-Pacific case, where a naive fixed-radius
    box returns nothing and the old code fell back to a full scan)."""
    lat, lon = -48.0, -160.0
    got = C.get_nearest_city(lat, lon)
    want = _exhaustive(synthetic_db, lat, lon)
    assert got is not None
    assert (got["name"], round(got["distance_km"], 6)) == want


def test_touches_far_fewer_cities_than_a_full_scan(synthetic_db):
    """Guards the actual purpose: if a change silently reverts to scanning
    everything, the answers stay right but the render thread stalls again."""
    calls = {"n": 0}
    real = C._haversine_km

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    C._haversine_km = counting
    try:
        C._grid = None
        C._grid_src = None
        C.get_nearest_city(47.68, -118.87)
    finally:
        C._haversine_km = real
    assert calls["n"] < len(synthetic_db) / 4, (
        f"examined {calls['n']} of {len(synthetic_db)} cities — the index is not "
        f"pruning; expect a render-thread stall on real data")


def test_distance_is_a_real_great_circle_value(synthetic_db):
    """Sanity: the reported distance must match haversine to the named city."""
    lat, lon = 47.68, -118.87
    got = C.get_nearest_city(lat, lon)
    match = [c for c in synthetic_db if c[0] == got["name"]][0]
    assert math.isclose(got["distance_km"],
                        C._haversine_km(lat, lon, match[1], match[2]),
                        rel_tol=1e-9)


def test_stopping_bound_is_a_true_great_circle_lower_bound():
    """The bound must not use flat degrees-along-a-parallel.

    Degrees of longitude measured along a parallel OVERSTATE how far the meridian
    really is (a great circle cuts poleward), so `ring * 111 * cos(lat)` is not a
    lower bound and the search stops too early. This exact two-city arrangement made
    the first version return the 2190km city instead of the 2174km one.

    A dense uniform fixture cannot catch this — rings stay at 0-2 where the slack
    dwarfs the error — so the sparseness and the ~40-ring separation are the point.
    """
    old = (C._db, C._loaded, C._grid, C._grid_src)
    try:
        _with_db([["decoy", 41.29, 0.999], ["hidden", 60.999, 42.001]])
        got = C.get_nearest_city(60.999, 0.999)
        want = _exhaustive(C._db, 60.999, 0.999)
        assert (got["name"], round(got["distance_km"], 6)) == want
        assert got["name"] == "hidden", (
            f"stopped early and returned {got['name']} — the bound is not a true "
            f"lower bound")
    finally:
        C._db, C._loaded, C._grid, C._grid_src = old


def test_sparse_high_latitude_probes_match_exhaustive():
    """Property test in the regime where the bound actually gets exercised:
    few cities, mid-to-high latitude, so rings grow large."""
    old = (C._db, C._loaded, C._grid, C._grid_src)
    try:
        random.seed(2024)
        for _ in range(400):
            db = [[f"c{i}", random.uniform(-89, 89), random.uniform(-180, 180)]
                  for i in range(random.randint(5, 20))]
            _with_db(db)
            lat = random.choice([1, -1]) * random.uniform(55, 82)
            lon = random.uniform(-180, 180)
            got = C.get_nearest_city(lat, lon)
            want = _exhaustive(db, lat, lon)
            got_t = (got["name"], round(got["distance_km"], 6)) if got else None
            assert got_t == want, f"sparse mismatch at {lat:.2f},{lon:.2f}"
    finally:
        C._db, C._loaded, C._grid, C._grid_src = old


def test_city_at_exactly_positive_180_is_reachable():
    """Buckets must be wrapped the same way lookups are, or a city at lon == +180
    lands in a bucket that is never examined."""
    old = (C._db, C._loaded, C._grid, C._grid_src)
    try:
        # Filler far away so the exhaustive fallback is expensive and therefore
        # DISTINGUISHABLE: with only two cities the fallback also costs two
        # haversines, and the assertion below could not tell the paths apart.
        filler = [[f"f{i}", -60.0 + i * 0.5, -100.0 + i * 0.3] for i in range(60)]
        _with_db([["e180", 0.0, 180.0]] + filler)
        # If the bucket key is not wrapped, e180 sits in a bucket no ring visits,
        # the ring search never meets its bound, and the exhaustive fallback
        # rescues the ANSWER while silently costing a full scan — so asserting the
        # name alone cannot see the bug. Count the work instead.
        calls = {"n": 0}
        real = C._haversine_km

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        C._haversine_km = counting
        try:
            got = C.get_nearest_city(0.0, 179.9)
        finally:
            C._haversine_km = real
        assert got["name"] == "e180"
        assert calls["n"] < 10, (
            f"{calls['n']} haversines over a {len(C._db)}-city db — the +180 bucket "
            f"was missed and the exhaustive fallback ran")
    finally:
        C._db, C._loaded, C._grid, C._grid_src = old


def test_grid_snapshots_the_list_it_indexes():
    """_grid_src must record the list actually indexed, not whatever _db points at
    when the build finishes — otherwise a swap mid-build installs a stale grid and
    labels it fresh, and the identity guard never self-heals."""
    old = (C._db, C._loaded, C._grid, C._grid_src)
    try:
        first = [["alpha", 10.0, 10.0]]
        _with_db(first)
        C._build_grid()
        assert C._grid_src is first
        second = [["beta", 10.0, 10.0]]
        C._db = second
        C._build_grid()
        assert C._grid_src is second
        assert C.get_nearest_city(10.0, 10.01)["name"] == "beta"

        # And the snapshot must be taken BEFORE the walk: a list that swaps _db
        # while it is being iterated must still label the grid with what was
        # actually indexed, or the guard marks stale data as fresh forever.
        class SwapsDuringIteration(list):
            def __iter__(self):
                C._db = [["gamma", 10.0, 10.0]]
                return super().__iter__()
        tricky = SwapsDuringIteration([["delta", 10.0, 10.0]])
        C._db = tricky
        C._grid = None
        C._grid_src = None
        C._build_grid()
        assert C._grid_src is tricky, (
            "_grid_src was read from the global after the build, so a mid-build "
            "swap installs a stale grid labelled fresh")
    finally:
        C._db, C._loaded, C._grid, C._grid_src = old
