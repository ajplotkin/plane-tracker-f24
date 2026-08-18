"""
test_landmark_state_and_water.py — step 3 of the landmark chain now names the
state/country, and open water is named as water.

Two behaviours this pins, both of which were silently wrong before:

* cities.json (step 3) returned a BARE name, so it disagreed with Nominatim
  (step 2) for the same place — "Ephrata" vs "Ephrata, WA" — and cities5000 has
  four US Parises and three Bostons, so the bare name was genuinely ambiguous.

* Steps 4 and 5 were unreachable. get_nearest_city() only returns None when the
  database failed to LOAD, never because the nearest city is far, so a plane
  mid-Atlantic was labelled "nr Ribeira Grande" for a town 1079km away, rendered
  exactly like a town it was flying over.
"""

import pytest

from utilities import landmarks as lm
from utilities import cities


@pytest.fixture
def nom():
    """Drive the Nominatim cache directly and always restore it."""
    saved = (lm._nom_city, lm._nom_country, lm._nom_resolved,
             lm._nom_query_lat, lm._nom_query_lon, lm._nom_fetching,
             lm._parks_db, lm._parks_loaded)

    def _set(city=None, country=None, resolved=True):
        lm._nom_city, lm._nom_country, lm._nom_resolved = city, country, resolved
        lm._nom_query_lat, lm._nom_query_lon = 0.0, 0.0
        lm._nom_fetching = True      # suppress the background refetch
        lm._parks_db, lm._parks_loaded = [], True   # never touch the NPS API
    yield _set
    (lm._nom_city, lm._nom_country, lm._nom_resolved, lm._nom_query_lat,
     lm._nom_query_lon, lm._nom_fetching, lm._parks_db,
     lm._parks_loaded) = saved


# ---------------------------------------------------------------------------
# admin1 -> state/province
# ---------------------------------------------------------------------------

def test_us_admin1_is_already_the_state_code():
    assert lm._state_from_admin1("US", "WA") == "WA"
    assert lm._state_from_admin1("us", "NY") == "NY"


def test_canada_admin1_is_numeric_and_must_be_mapped():
    """GeoNames ships Ontario as "08". Returning admin1 unchanged — the obvious
    implementation — renders "Toronto, 08"."""
    assert lm._state_from_admin1("CA", "08") == "ON"
    assert lm._state_from_admin1("CA", "10") == "QC"
    assert lm._state_from_admin1("CA", "12") == "YT"
    assert lm._state_from_admin1("CA", "08") != "08"


def test_other_countries_get_no_state():
    """Non-US/CA admin1 is a region number that means nothing on a 64px panel —
    "Reykjavik, 39". _format_city_name uses the country code for these instead."""
    assert lm._state_from_admin1("IS", "39") == ""
    assert lm._state_from_admin1("FR", "11") == ""
    assert lm._state_from_admin1("DE", "05") == ""


def test_malformed_admin1_degrades_to_no_state():
    assert lm._state_from_admin1("US", "") == ""
    assert lm._state_from_admin1("US", "123") == ""   # not a 2-letter code
    assert lm._state_from_admin1(None, None) == ""
    assert lm._state_from_admin1("CA", "99") == ""    # not a real province code


# ---------------------------------------------------------------------------
# cities.json carries the codes
# ---------------------------------------------------------------------------

def test_cache_version_forces_a_rebuild_of_v1_caches():
    """v1 rows are [name, lat, lon] with no codes. Without the bump, devices keep
    a v1 cache forever and step 3 silently never gains the state."""
    assert cities.CACHE_VERSION >= 2


def test_nearest_city_returns_country_and_admin1():
    c = cities.get_nearest_city(47.68, -118.87)
    assert c is not None
    assert c["country"] == "US"
    assert c["admin1"] == "WA"


def test_short_rows_do_not_raise():
    """v1 caches and older fixtures build 3-element rows."""
    assert cities._row_fields(["X", 1.0, 2.0]) == ("X", 1.0, 2.0, "", "")


def test_codes_are_interned():
    """Without interning the two extra fields cost 139,258 separate str objects
    (+8.76MB heap measured) instead of ~4,000 (+1.15MB). These are 512MB Pis and
    heap pressure is the whole reason this subsystem was rewritten."""
    cities._load()
    if len(cities._db) < 1000 or len(cities._db[0]) < 5:
        pytest.skip("no full v2 database present")
    distinct_objects = len({id(r[3]) for r in cities._db})
    assert distinct_objects < 300, (
        f"{distinct_objects} distinct country-code objects — interning is not "
        f"being applied on the cache-load path")


# ---------------------------------------------------------------------------
# step 3 formatting
# ---------------------------------------------------------------------------

def test_step3_appends_us_state(nom):
    """The bare-name regression: 'Ephrata' alone is the WA one or the PA one."""
    nom(city=None, country="United States", resolved=True)
    r = lm.get_nearest_landmark(47.68, -118.87)
    assert r["type"] == "city"
    assert r["name"] == "Ephrata, WA"


def test_step3_appends_country_code_outside_north_america(nom):
    nom(city=None, country="Iceland", resolved=True)
    r = lm.get_nearest_landmark(64.5, -20.5)
    assert r["name"].endswith(", IS")


def test_step3_never_renders_a_numeric_region(nom):
    """Guards the shortcut of passing admin1 straight through."""
    nom(city=None, country="France", resolved=True)
    r = lm.get_nearest_landmark(46.5, 2.5)
    tail = r["name"].split(", ")[-1]
    assert not tail.isdigit(), f"numeric region code leaked into {r['name']!r}"


def test_step2_still_wins_over_step3(nom):
    nom(city="East Hampton, NY", country="United States", resolved=True)
    assert lm.get_nearest_landmark(40.96, -72.18)["name"] == "East Hampton, NY"


# ---------------------------------------------------------------------------
# water
# ---------------------------------------------------------------------------

def test_open_water_is_named_as_water_not_as_a_distant_town(nom):
    nom(city=None, country=None, resolved=True)
    r = lm.get_nearest_landmark(35.0, -40.0)
    assert r["type"] == "ocean"
    assert r["name"] == "North Atlantic"


def test_unresolved_lookup_must_not_claim_water(nom):
    """_nom_country is None both over water AND before the first fetch returns.
    Dropping the _nom_resolved guard makes every land requery flash an ocean
    name — and the naive version passes every other test in this file."""
    nom(city=None, country=None, resolved=False)
    r = lm.get_nearest_landmark(47.68, -118.87)
    assert r["type"] == "city", "an unresolved lookup over land was called water"
    assert r["name"] == "Ephrata, WA"


def test_land_with_a_country_is_never_called_water(nom):
    """Even 613km from the nearest city, the outback is land."""
    nom(city=None, country="Australia", resolved=True)
    assert lm.get_nearest_landmark(-25.0, 128.0)["type"] == "city"


def test_water_name_is_total():
    """The rectangle list left 115 open-water probes unnamed on a 5-degree sweep,
    including the whole SE Pacific and the North Pacific west of the dateline."""
    missing = [(la, lo) for la in range(-85, 86, 5) for lo in range(-180, 180, 5)
               if lm._water_name(la, lo) is None]
    assert missing == []


@pytest.mark.parametrize("lat,lon,expected", [
    (30, 150, "North Pacific"),      # Tokyo-Hawaii; old box stopped at -80
    (-30, -80, "South Pacific"),     # off Chile; fell in the -100..-70 gap
    (-45, 130, "Indian Ocean"),      # south of Australia
    (45, -6, "Bay of Biscay"),       # was "Mediterranean Sea"
    (46, -3, "Bay of Biscay"),       # was "Mediterranean Sea"
    (10, -85, "North Pacific"),      # PACIFIC side of Costa Rica; was Caribbean
    (12, -82, "Caribbean Sea"),      # real Caribbean still works
    (20, -86, "Gulf of Mexico"),
    (35, -40, "North Atlantic"),
    (38, 15, "Mediterranean Sea"),
])
def test_water_names_at_known_positions(lat, lon, expected):
    assert lm._water_name(lat, lon) == expected


def test_get_ocean_name_stays_partial():
    """_water_name is total; _get_ocean_name is not, and callers (step 5) rely on
    None to mean 'no box matched'. Making the box list total would break that."""
    assert lm._get_ocean_name(23.0, 10.0) is None
