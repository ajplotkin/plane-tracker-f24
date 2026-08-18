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
def nom(monkeypatch):
    """Drive the Nominatim cache directly and always restore it.

    Also hard-blocks the network: the suppression used to rest solely on setting
    _nom_fetching, so removing the `not _nom_fetching` guard in
    _ensure_nominatim would have left the suite quietly hitting live Nominatim
    instead of failing.
    """
    def _no_network(*a, **k):
        raise AssertionError("test attempted a live Nominatim request")
    monkeypatch.setattr(lm.requests, "get", _no_network)
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


# ---------------------------------------------------------------------------
# name truncation
# ---------------------------------------------------------------------------

def test_truncation_ends_on_a_whole_word():
    """A plain hard slice cut mid-word for 518 of the 69,629 cities."""
    assert lm._truncate_name("Dubai International Financial Centre") == "Dubai International"
    assert (lm._truncate_name("Borgoricco-San Michele delle Badesse-Sant'Eufemia")
            == "Borgoricco-San Michele")


def test_truncation_does_not_leave_dangling_punctuation():
    """Breaking after a comma renders "United Townships of Dysart," — a list
    introduced and then cut off."""
    out = lm._truncate_name(
        "United Townships of Dysart, Dudley, Harcourt, Guilford, Harburn")
    assert out == "United Townships of Dysart"
    assert not out.endswith((",", " ", "-", ".", ";", ":"))


def test_truncation_leaves_short_names_alone():
    assert lm._truncate_name("Ephrata") == "Ephrata"
    assert lm._truncate_name("x" * lm.MAX_NAME_LEN) == "x" * lm.MAX_NAME_LEN


def test_truncation_falls_back_to_a_hard_cut_for_one_long_word():
    """No boundary to break on — a hard slice is the only option, and it must
    still respect the limit rather than returning the whole name."""
    out = lm._truncate_name("Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch")
    assert len(out) == lm.MAX_NAME_LEN


def test_truncation_never_exceeds_the_limit_anywhere_in_the_database():
    cities._load()
    if len(cities._db) < 1000:
        pytest.skip("no full database present")
    for row in cities._db:
        name, _, _, country, admin1 = cities._row_fields(row)
        out = lm._format_city_name(name, lm._state_from_admin1(country, admin1), country)
        assert len(out) <= lm.MAX_NAME_LEN, f"{name!r} -> {out!r}"


def test_truncation_does_not_collapse_to_a_stub_on_an_early_boundary():
    """The boundary has to be far enough in to still name the place. Accepting
    any boundary at all turns "A Extraordinarilylongplacename" into "A" — and
    that mutation passed every other test in this file."""
    out = lm._truncate_name("A Extraordinarilylongplacenamehere")
    assert len(out) > lm.MAX_NAME_LEN // 2, f"collapsed to {out!r}"


def test_truncation_never_leaves_a_trailing_separator():
    """Must stay LONGER than MAX_NAME_LEN or this asserts nothing — the original
    27-char example silently went vacuous when the cap moved 24 -> 28."""
    name = "Saint-Basile-le-Grand-Sur-Richelieu-Extra"
    assert len(name) > lm.MAX_NAME_LEN
    assert not lm._truncate_name(name).endswith(("-", " ", ",", "/"))


# ---------------------------------------------------------------------------
# state table completeness
# ---------------------------------------------------------------------------

def test_every_canadian_city_resolves_a_province():
    """All 1,322 CA rows must map; a missing code renders "Toronto, CA"."""
    cities._load()
    if len(cities._db) < 1000 or len(cities._db[0]) < 5:
        pytest.skip("no full v2 database present")
    ca = [r for r in cities._db if r[3] == "CA"]
    assert len(ca) > 1000
    unresolved = [r[0] for r in ca if not lm._state_from_admin1(r[3], r[4])]
    assert unresolved == []


def test_ca_province_table_matches_geonames():
    """Pinned from admin1CodesASCII.txt, not inferred from sample cities."""
    assert lm._CA_PROVINCE == {
        "01": "AB", "02": "BC", "03": "MB", "04": "NB", "05": "NL",
        "07": "NS", "08": "ON", "09": "PE", "10": "QC", "11": "SK",
        "12": "YT", "13": "NT", "14": "NU",
    }


def test_nominatim_fallback_table_covers_dc_and_canada():
    """ISO3166-2-lvl4 is the primary path; this table is the backup and was
    US-states-only, so a Canadian address without the ISO code fell through to
    the bare country code."""
    assert lm._STATE_ABBR.get("District of Columbia") == "DC"
    assert lm._STATE_ABBR.get("Ontario") == "ON"
    assert lm._STATE_ABBR.get("Nunavut") == "NU"
    assert lm._STATE_ABBR.get("Québec") == "QC", "Nominatim returns the accented form"



# ---------------------------------------------------------------------------
# long settlement names must be trimmed, not thrown away
# ---------------------------------------------------------------------------

def test_long_settlement_name_is_truncated_not_discarded(monkeypatch, nom):
    """_nominatim_fetch used to `continue` past any name over MAX_NAME_LEN,
    silently dropping the CORRECT settlement and falling through to the next
    address key — or to the cities.json nearest-neighbour, a different town
    kilometres away. A long name is still the right answer; it needs trimming.
    """
    long_name = "Dubai International Financial Centre"

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"address": {"city": long_name, "country_code": "ae"}}

    monkeypatch.setattr(lm.requests, "get", lambda *a, **k: _Resp())
    nom(city=None, country=None, resolved=False)
    lm._nom_fetching = False
    lm._nominatim_fetch(25.2, 55.3)

    assert lm._nom_city is not None, "the long settlement name was discarded"
    assert lm._nom_city.startswith("Dubai International")
    assert len(lm._nom_city) <= lm.MAX_NAME_LEN


def test_a_shorter_later_key_does_not_win_over_a_long_city(monkeypatch, nom):
    """The keys are ordered most-specific-first. Discarding a long `city` let a
    vaguer `suburb` answer instead."""
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"address": {"city": "Dubai International Financial Centre",
                                "suburb": "Zabeel", "country_code": "ae"}}

    monkeypatch.setattr(lm.requests, "get", lambda *a, **k: _Resp())
    nom(city=None, country=None, resolved=False)
    lm._nom_fetching = False
    lm._nominatim_fetch(25.2, 55.3)
    assert lm._nom_city.startswith("Dubai"), (
        f"a later, vaguer key won: {lm._nom_city!r}")



# ---------------------------------------------------------------------------
# findings from the audit
# ---------------------------------------------------------------------------

def test_the_caspian_is_not_the_indian_ocean():
    """The Caspian is international water with no admin polygon, so Nominatim
    answers "Unable to geocode" — which _over_water() reads as open sea. The
    basin fallback then named it the Indian Ocean, ~1500km away, for an entire
    Europe-India crossing. (Superior, Baikal, Victoria and Great Bear all sit
    inside national polygons, so Nominatim names a country and they never reach
    this code.)"""
    assert lm._water_name(42.0, 50.5) == "Caspian Sea"
    assert lm._water_name(46.0, 51.0) == "Caspian Sea"
    # and it must not have eaten its neighbours
    assert lm._water_name(26.0, 52.0) == "Persian Gulf"
    assert lm._water_name(10.0, 70.0) == "Arabian Sea"


def test_truncation_strips_a_dangling_slash():
    """27 GeoNames rows are Swiss names like "Zuerich (Kreis 11) / Oberstrass",
    which cut to "Zuerich (Kreis 11) /"."""
    assert lm._truncate_name("Zuerich (Kreis 11) / Oberstrass") == "Zuerich (Kreis 11)"


def test_a_failed_lookup_drops_the_water_claim(monkeypatch, nom):
    """_nom_resolved must not survive an error. The water test is "a COMPLETED
    lookup found no country", and an error completed nothing. Leaving it set
    pinned the last verdict onto every later position: resolve mid-Atlantic,
    cross the Irish coast into a Nominatim outage, and the display reports
    "North Atlantic" over County Kerry, re-confirming every 15km until the
    outage ends."""
    nom(city=None, country=None, resolved=True)
    assert lm._over_water()

    def boom(*a, **k):
        raise RuntimeError("nominatim unreachable")
    monkeypatch.setattr(lm.requests, "get", boom)
    lm._nom_fetching = False
    lm._nominatim_fetch(52.0, -9.5)          # County Kerry, Ireland

    assert not lm._over_water(), (
        "a failed lookup left the ocean verdict pinned onto a land position")


def test_admin1_codes_are_interned_as_well_as_country():
    """Only the country intern was pinned; dropping the admin1 one survived the
    whole suite and silently gives back about half the memory saving."""
    cities._load()
    if len(cities._db) < 1000 or len(cities._db[0]) < 5:
        pytest.skip("no full v2 database present")
    assert len({id(r[4]) for r in cities._db}) < 2000, (
        "admin1 codes are not interned")


def test_formatter_truncates_at_a_boundary_not_a_hard_slice():
    """The production paths call _format_city_name, never _truncate_name
    directly. Replacing the formatter's two overflow branches with
    candidate[:MAX_NAME_LEN].rstrip() survived the entire suite while rendering
    "Notre-Dame-de-l'Ile-Perrot," with a trailing comma."""
    # US/CA branch: name + ", QC" overflows, the bare name does not
    out = lm._format_city_name("Notre-Dame-de-l'Ile-Perrot", "QC", "ca")
    assert out == "Notre-Dame-de-l'Ile-Perrot"
    # other-country branch: the name itself overflows
    assert lm._format_city_name(
        "Dubai International Financial Centre", "", "ae") == "Dubai International"


@pytest.mark.parametrize("lat,lon,expected", [
    (66.0, 0.0, "Arctic Ocean"),        # exact Arctic threshold
    (65.9, 0.0, "North Atlantic"),      # just below it
    (0.0, -40.0, "North Atlantic"),     # exact equator counts as north
    (-0.1, -40.0, "South Atlantic"),
    (-55.0, 0.0, "Southern Ocean"),     # exact Southern threshold
    (-54.9, 0.0, "South Atlantic"),
    (-30.0, -70.0, "South Atlantic"),   # Cape Horn split
    (-30.0, -70.01, "South Pacific"),
    (10.0, -80.0, "North Atlantic"),    # Panama split
    (10.0, -80.01, "North Pacific"),
])
def test_ocean_basin_boundaries(lat, lon, expected):
    """Pinned on _ocean_basin directly: at _water_name level a sea box masks
    several of these, so mutating the 66-degree Arctic threshold survived."""
    assert lm._ocean_basin(lat, lon) == expected


def test_truncation_boundary_is_exactly_half_the_limit():
    half = lm.MAX_NAME_LEN // 2
    at = "x" * half + " " + "y" * 40
    below = "x" * (half - 1) + " " + "y" * 40
    assert lm._truncate_name(at) == "x" * half, "a boundary at exactly MAX//2 must be used"
    assert lm._truncate_name(below) == below[:lm.MAX_NAME_LEN], "below MAX//2 must hard-slice"


def test_refresh_survives_a_cache_it_cannot_delete():
    """os.remove needs write permission on the DIRECTORY, which the daemon user
    lacks; the file's own 0666 mode is irrelevant. refresh() used to raise, and
    would also have re-read the surviving cache and done nothing."""
    saved = (cities._db, cities._loaded, cities._grid, cities._grid_src)
    fake = [["Testville", 1.0, 2.0, "US", "NY"]]
    try:
        import utilities.cities as C
        orig_exists, orig_remove, orig_build = C.os.path.exists, C.os.remove, C._download_and_build

        def denied(_p):
            raise PermissionError("Operation not permitted")
        C.os.path.exists = lambda _p: True
        C.os.remove = denied
        C._download_and_build = lambda: fake
        try:
            C.refresh()
        finally:
            C.os.path.exists, C.os.remove, C._download_and_build = orig_exists, orig_remove, orig_build
        assert cities._db == fake, "refresh() did not rebuild when the unlink failed"
    finally:
        cities._db, cities._loaded, cities._grid, cities._grid_src = saved



def test_boundary_at_exactly_half_uses_a_real_city():
    """Synthetic x/y strings pin the arithmetic; these are the two real cities
    whose only boundary sits exactly at MAX_NAME_LEN//2, so `>=` becoming `>`
    renders them mid-word."""
    assert lm._truncate_name("San Bernardino Tlaxcalancingo") == "San Bernardino"
    assert lm._truncate_name("Nuevo San Juan Parangaricutiro") == "Nuevo San Juan"


def test_hyphen_alone_is_a_usable_boundary():
    """Every other truncation example here also contains a space, so dropping
    the hyphen from the boundary search passed the whole suite. 15 real cities
    have the hyphen as their ONLY usable boundary — mostly Montreal and Toronto
    boroughs, which is exactly the Canadian naming this work started from."""
    assert lm._truncate_name("Mercier-Hochelaga-Maisonneuve") == "Mercier-Hochelaga"
    assert lm._truncate_name("Saint-Maximin-la-Sainte-Baume") == "Saint-Maximin-la-Sainte"


def test_hard_slice_branch_also_strips_trailing_punctuation():
    """The no-usable-boundary branch has its own rstrip; removing it passed
    everything because no city in the database can reach that branch."""
    name = "x" * (lm.MAX_NAME_LEN - 1) + "." + "y" * 6
    assert lm._truncate_name(name) == "x" * (lm.MAX_NAME_LEN - 1)


def test_truncation_never_returns_an_empty_name():
    """An empty name renders as a bare "nr " on the panel. Unreachable from the
    real data sources, but the park path now feeds arbitrary NPS strings in."""
    for pathological in ["-" * 40, " " * 40, ",.;:/ " * 8, "- " * 20]:
        assert lm._truncate_name(pathological) != ""


def test_park_names_respect_the_cap(nom):
    """Parks bypassed MAX_NAME_LEN entirely: 94 of the 474 NPS names exceed it
    even after suffix-stripping, up to 65 chars, and a name that long holds the
    scroll far longer than any city name is permitted to."""
    saved = (lm._parks_db, lm._parks_loaded)
    try:
        nom(city=None, country="United States", resolved=True)
        lm._parks_db = [[
            "Washington-Rochambeau Revolutionary Route National Historic Trail",
            40.96, -72.18]]
        lm._parks_loaded = True
        r = lm.get_nearest_landmark(40.96, -72.18)
        assert r["type"] == "park"
        assert len(r["name"]) <= lm.MAX_NAME_LEN, f"park name unbounded: {r['name']!r}"
        assert r["name"] == "Washington-Rochambeau"
    finally:
        lm._parks_db, lm._parks_loaded = saved
