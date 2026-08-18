"""
landmarks.py — Nearest landmark lookup combining NPS parks, Nominatim, and cities.

Priority chain:
  1. NPS parks within 30km — named national/state parks from NPS API.
  2. Nominatim reverse geocode — background thread, re-queried every 15km of movement.
  3. Local cities.json nearest-neighbour — via utilities/cities.py, formatted
     with state/country so it matches step 2 ("Ephrata, WA", "Paris, FR").
     Skipped over open water, where the nearest city can be 1000km+ away.
  4. Country name from country_code — for remote land areas.
  5. Ocean/sea name from coordinates — bounding-box lookup over water.

NPS parks downloaded from https://developer.nps.gov/api/v1/parks (paginated).
Cached as nationalparks.json. Requires NPS_API_KEY env var.

GeoNames cities via existing utilities/cities.py.
Nominatim queried in the background every REQUERY_AFTER_KM of movement.
"""

import json
import logging
import os
import threading

import requests

try:
    from utilities.api_usage import log_call as _log_api
except ImportError:
    _log_api = lambda source: None

from utilities.cities import get_nearest_city, _haversine_km

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
PARKS_CACHE = os.path.join(BASE_DIR, "nationalparks.json")

NPS_API_URL = "https://developer.nps.gov/api/v1/parks"
PARKS_RADIUS_KM = 30  # Only show park name if plane is within this distance
REQUERY_AFTER_KM = 15  # Re-query Nominatim after this much movement
# Cap on a rendered place name. The stats line SCROLLS, so this is not about
# fitting the panel -- nothing is clipped -- it bounds how long one name holds
# the scroll before it comes round again. Measured over the 69,629-city
# database: median name is 8 chars, p90 15, p99 24, but the tail runs to 97
# ("United Townships of Dysart, Dudley, Harcourt, ..." in Ontario), so a cap is
# needed. 28 sits just past p99 and keeps the suffix on names like
# "Saint-Jean-sur-Richelieu, QC" that 24 truncated away.
MAX_NAME_LEN = 28

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOM_HEADERS = {"User-Agent": "plane-tracker-rgb-pi/1.0"}

# Suffixes to strip from park full names for display
_STRIP_SUFFIXES = [
    " National Park and Preserve",
    " National Park & Preserve",
    " National Historical Park",
    " National Historic Site",
    " National Monument and Preserve",
    " National Monument & Preserve",
    " National Monument",
    " National Seashore",
    " National Lakeshore",
    " National Recreation Area",
    " National Preserve",
    " National Park",
    " National Battlefield",
    " National Memorial",
    " National Scenic River",
    " National River",
    " National Wild and Scenic River",
]

# ---------------------------------------------------------------------------
# Country code -> English name lookup
# ---------------------------------------------------------------------------

_COUNTRY_NAMES = {
    "af": "Afghanistan", "al": "Albania", "dz": "Algeria", "ad": "Andorra",
    "ao": "Angola", "ar": "Argentina", "am": "Armenia", "au": "Australia",
    "at": "Austria", "az": "Azerbaijan", "bs": "Bahamas", "bh": "Bahrain",
    "bd": "Bangladesh", "by": "Belarus", "be": "Belgium", "bz": "Belize",
    "bj": "Benin", "bt": "Bhutan", "bo": "Bolivia", "ba": "Bosnia",
    "bw": "Botswana", "br": "Brazil", "bn": "Brunei", "bg": "Bulgaria",
    "bf": "Burkina Faso", "bi": "Burundi", "kh": "Cambodia", "cm": "Cameroon",
    "ca": "Canada", "cf": "C. African Rep.", "td": "Chad", "cl": "Chile",
    "cn": "China", "co": "Colombia", "cg": "Congo", "cd": "DR Congo",
    "cr": "Costa Rica", "hr": "Croatia", "cu": "Cuba", "cy": "Cyprus",
    "cz": "Czech Republic", "dk": "Denmark", "dj": "Djibouti", "do": "Dominican Rep.",
    "ec": "Ecuador", "eg": "Egypt", "sv": "El Salvador", "er": "Eritrea",
    "ee": "Estonia", "et": "Ethiopia", "fj": "Fiji", "fi": "Finland",
    "fr": "France", "ga": "Gabon", "gm": "Gambia", "ge": "Georgia",
    "de": "Germany", "gh": "Ghana", "gr": "Greece", "gl": "Greenland",
    "gt": "Guatemala", "gn": "Guinea", "gy": "Guyana", "ht": "Haiti",
    "hn": "Honduras", "hu": "Hungary", "is": "Iceland", "in": "India",
    "id": "Indonesia", "ir": "Iran", "iq": "Iraq", "ie": "Ireland",
    "il": "Israel", "it": "Italy", "jm": "Jamaica", "jp": "Japan",
    "jo": "Jordan", "kz": "Kazakhstan", "ke": "Kenya", "kp": "North Korea",
    "kr": "South Korea", "kw": "Kuwait", "kg": "Kyrgyzstan", "la": "Laos",
    "lv": "Latvia", "lb": "Lebanon", "ls": "Lesotho", "lr": "Liberia",
    "ly": "Libya", "lt": "Lithuania", "lu": "Luxembourg", "mg": "Madagascar",
    "mw": "Malawi", "my": "Malaysia", "mv": "Maldives", "ml": "Mali",
    "mt": "Malta", "mr": "Mauritania", "mx": "Mexico", "md": "Moldova",
    "mn": "Mongolia", "me": "Montenegro", "ma": "Morocco", "mz": "Mozambique",
    "mm": "Myanmar", "na": "Namibia", "np": "Nepal", "nl": "Netherlands",
    "nz": "New Zealand", "ni": "Nicaragua", "ne": "Niger", "ng": "Nigeria",
    "no": "Norway", "om": "Oman", "pk": "Pakistan", "pa": "Panama",
    "pg": "Papua New Guinea", "py": "Paraguay", "pe": "Peru",
    "ph": "Philippines", "pl": "Poland", "pt": "Portugal", "qa": "Qatar",
    "ro": "Romania", "ru": "Russia", "rw": "Rwanda", "sa": "Saudi Arabia",
    "sn": "Senegal", "rs": "Serbia", "sl": "Sierra Leone", "sg": "Singapore",
    "sk": "Slovakia", "si": "Slovenia", "so": "Somalia", "za": "South Africa",
    "ss": "South Sudan", "es": "Spain", "lk": "Sri Lanka", "sd": "Sudan",
    "sr": "Suriname", "se": "Sweden", "ch": "Switzerland", "sy": "Syria",
    "tw": "Taiwan", "tj": "Tajikistan", "tz": "Tanzania", "th": "Thailand",
    "tl": "Timor-Leste", "tg": "Togo", "to": "Tonga", "tt": "Trinidad",
    "tn": "Tunisia", "tr": "Turkey", "tm": "Turkmenistan", "ug": "Uganda",
    "ua": "Ukraine", "ae": "UAE", "gb": "United Kingdom", "us": "United States",
    "uy": "Uruguay", "uz": "Uzbekistan", "ve": "Venezuela", "vn": "Vietnam",
    "ye": "Yemen", "zm": "Zambia", "zw": "Zimbabwe",
    "aq": "Antarctica", "eh": "Western Sahara", "ps": "Palestine",
    "xk": "Kosovo", "cv": "Cape Verde", "km": "Comoros",
}


def _country_name(country_code):
    """Look up English country name from ISO 3166-1 alpha-2 code."""
    if not country_code:
        return None
    name = _COUNTRY_NAMES.get(country_code.lower())
    if not name:
        return None
    return _truncate_name(name)


# ---------------------------------------------------------------------------
# Ocean/sea detection — coordinate-based bounding boxes
# Ordered most-specific first (seas before oceans).
# ---------------------------------------------------------------------------

_OCEAN_REGIONS = [
    # Seas and gulfs (more specific, checked first)
    # West edge is -84, not -87: at -85..-87 this box was claiming the PACIFIC
    # coast of Costa Rica and Nicaragua. Yucatan waters west of -84 are picked
    # up by the Gulf of Mexico box instead. The Gulf of Panama (around 8N, -79)
    # is still misread as Caribbean — the isthmus runs east-west there, so no
    # rectangle and no single meridian can separate its two coasts.
    ("Caribbean Sea",       (8,  26, -84, -59)),
    ("Gulf of Mexico",      (18, 31, -98, -80)),
    ("Bay of Biscay",        (43, 49, -12,  -1)),
    ("English Channel",      (49, 51,  -6,   2)),
    ("Mediterranean Sea",   (30, 47,  -6,  37)),
    ("North Sea",           (51, 62,  -4,  13)),
    ("Baltic Sea",          (53, 66,  10,  30)),
    ("Black Sea",           (41, 47,  28,  42)),
    # The Caspian is the one large lake that reaches this code. Nominatim
    # returns "Unable to geocode" mid-Caspian (international water, no admin
    # polygon covering it), which _over_water() reads as open sea -- and the
    # basin fallback then called it the Indian Ocean, ~1500km away. Superior,
    # Baikal, Victoria and Great Bear all sit inside national polygons, so
    # Nominatim names a country for them and they never get here.
    ("Caspian Sea",         (36, 47,  46,  55)),
    ("Red Sea",             (12, 30,  32,  44)),
    ("Persian Gulf",        (22, 30,  47,  57)),
    ("Arabian Sea",         (5,  26,  52,  78)),
    ("Bay of Bengal",       (5,  23,  78, 100)),
    ("South China Sea",     (0,  25, 100, 122)),
    ("East China Sea",      (24, 34, 120, 132)),
    ("Sea of Japan",        (34, 52, 128, 142)),
    ("Bering Sea",          (52, 66, 163, 180)),
    ("Gulf of Alaska",      (52, 62,-152,-130)),
    ("Hudson Bay",          (51, 66, -95, -65)),
    ("Coral Sea",           (-25, -8, 142, 160)),
    ("Tasman Sea",          (-48,-28, 150, 175)),
    ("Norwegian Sea",       (62, 78, -15,  30)),
    ("Barents Sea",         (68, 82,  15,  60)),
    ("Labrador Sea",        (50, 68, -65, -42)),
    # Oceans (broader, checked after seas)
    ("Arctic Ocean",        (70, 90, -180, 180)),
    ("Southern Ocean",      (-90,-55, -180, 180)),
    ("North Atlantic",      (0,  66, -80,   0)),
    ("South Atlantic",      (-55,  0, -70,  20)),
    ("North Pacific",       (0,  66,-180, -80)),
    ("South Pacific",       (-55,  0,-180,-100)),
    ("Indian Ocean",        (-55, 30,  20, 120)),
]


def _get_ocean_name(lat, lon):
    """Return ocean/sea name for coordinates, or None if outside every box.

    Deliberately partial: the boxes do not tile the globe, so continental
    interiors fall through. Callers that already KNOW the position is water want
    _water_name() instead, which never returns None.
    """
    for name, (lat_min, lat_max, lon_min, lon_max) in _OCEAN_REGIONS:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return name
    return None


def _ocean_basin(lat, lon):
    """Coarse basin name for a position already known to be over water.

    TOTAL by construction — every coordinate gets a name. The boxes above leave
    real gaps that a rectangle list cannot close: a globe sweep found 115
    open-water probes with no name at all, including the entire SE Pacific off
    Chile and the North Pacific west of the dateline (the Tokyo-Hawaii tracks),
    because the "North Pacific" box only ran -180..-80.

    The Atlantic/Pacific split cannot be one meridian — the Americas are in the
    way — so it follows the land: Panama (~-80) in the north, Cape Horn (~-70)
    in the south. Likewise the Indian Ocean reaches further east below the
    equator, around Australia's south coast (~147).
    """
    if lat >= 66:
        return "Arctic Ocean"
    if lat <= -55:
        return "Southern Ocean"
    if 20 <= lon < (120 if lat >= 0 else 147):
        return "Indian Ocean"
    if (-80 if lat >= 0 else -70) <= lon < 20:
        return "North Atlantic" if lat >= 0 else "South Atlantic"
    return "North Pacific" if lat >= 0 else "South Pacific"


def _water_name(lat, lon):
    """Named sea if one matches, else the basin. Never None."""
    return _get_ocean_name(lat, lon) or _ocean_basin(lat, lon)


# ---------------------------------------------------------------------------
# US/CA state abbreviation from Nominatim address
# ---------------------------------------------------------------------------

_STATE_ABBR = {
    "California": "CA", "Oregon": "OR", "Washington": "WA",
    "Nevada": "NV", "Arizona": "AZ", "Utah": "UT", "Idaho": "ID",
    "Montana": "MT", "Wyoming": "WY", "Colorado": "CO",
    "New Mexico": "NM", "Texas": "TX", "Florida": "FL",
    "Georgia": "GA", "North Carolina": "NC", "South Carolina": "SC",
    "Virginia": "VA", "West Virginia": "WV", "Tennessee": "TN",
    "Kentucky": "KY", "Ohio": "OH", "Indiana": "IN", "Illinois": "IL",
    "Michigan": "MI", "Wisconsin": "WI", "Minnesota": "MN",
    "Iowa": "IA", "Missouri": "MO", "Arkansas": "AR",
    "Louisiana": "LA", "Mississippi": "MS", "Alabama": "AL",
    "Pennsylvania": "PA", "New York": "NY", "New Jersey": "NJ",
    "Connecticut": "CT", "Massachusetts": "MA", "Vermont": "VT",
    "New Hampshire": "NH", "Maine": "ME", "Rhode Island": "RI",
    "Delaware": "DE", "Maryland": "MD", "Alaska": "AK", "Hawaii": "HI",
    "Kansas": "KS", "Nebraska": "NE", "South Dakota": "SD",
    "North Dakota": "ND", "Oklahoma": "OK", "District of Columbia": "DC",
    # Canadian provinces. Nominatim has returned ISO3166-2-lvl4 ("CA-ON") for
    # every Canadian position tested, including remote Nunavut, so this branch
    # should never be needed -- but it cost nothing and the table was US-only.
    # Quebec is listed under both spellings because Nominatim answers "Quebec"
    # here with the accented form.
    "Alberta": "AB", "British Columbia": "BC", "Manitoba": "MB",
    "New Brunswick": "NB", "Newfoundland and Labrador": "NL",
    "Nova Scotia": "NS", "Ontario": "ON", "Prince Edward Island": "PE",
    "Quebec": "QC", "Qu\u00e9bec": "QC", "Saskatchewan": "SK", "Yukon": "YT",
    "Northwest Territories": "NT", "Nunavut": "NU",
}


# GeoNames ships US states as the 2-letter code in admin1, but Canada as NUMERIC
# codes — Ontario is "08", so a bare admin1 would render "Toronto, 08". Mapping
# confirmed against cities5000 by sampling a city per code: 12->Whitehorse (YT),
# 13->Yellowknife (NT), 14->Iqaluit (NU), 09->Charlottetown (PE), 03->Brandon
# (MB), 02->Abbotsford (BC), 05->Bay Roberts (NL). ("06" is retired and absent.)
_CA_PROVINCE = {
    "01": "AB", "02": "BC", "03": "MB", "04": "NB", "05": "NL",
    "07": "NS", "08": "ON", "09": "PE", "10": "QC", "11": "SK",
    "12": "YT", "13": "NT", "14": "NU",
}


def _state_from_admin1(country_code, admin1):
    """2-letter state/province for a GeoNames admin1 code, or "" if there is no
    useful one.

    Only US and CA get one. Everywhere else admin1 is a region number that means
    nothing on a 64px panel ("Reykjavik, 39"), so we return "" and let
    _format_city_name fall back to the country code instead.
    """
    cc = (country_code or "").lower()
    a1 = (admin1 or "").strip()
    if cc == "us":
        return a1 if len(a1) == 2 and a1.isalpha() else ""
    if cc == "ca":
        return _CA_PROVINCE.get(a1, "")
    return ""


def _get_state_abbr(address):
    """Extract 2-letter state/province code from Nominatim address dict."""
    state = address.get("ISO3166-2-lvl4", "")
    if state and "-" in state:
        return state.split("-")[-1]
    return _STATE_ABBR.get(address.get("state", ""), "")


# Characters that must not be left dangling at a cut. "/" is here because
# GeoNames carries 27 Swiss names like "Zuerich (Kreis 11) / Oberstrass",
# which cut to "Zuerich (Kreis 11) /".
_TRAILING_JUNK = " -,;:./("


def _truncate_name(name):
    """Trim to MAX_NAME_LEN, ending on a whole word.

    A plain slice cuts mid-word: 518 of the 69,629 cities in the database
    rendered like "Dubai International Fina" and "Notre-Dame-de-l'Ile-Perr".
    Prefer the last space or hyphen inside the limit; fall back to the hard slice
    only when that would leave too little to identify the place (a single very
    long word).
    """
    if len(name) <= MAX_NAME_LEN:
        return name
    head = name[:MAX_NAME_LEN]
    cut = max(head.rfind(" "), head.rfind("-"))
    if cut >= MAX_NAME_LEN // 2:
        # Strip punctuation too, or a name that breaks after a comma renders
        # "United Townships of Dysart," with the list it introduced cut off.
        out = head[:cut].rstrip(_TRAILING_JUNK)
    else:
        out = head.rstrip(_TRAILING_JUNK)
    # Never hand back "": a name made entirely of separators would render as a
    # bare "nr " on the panel. head is non-empty here by construction.
    return out or head


def _format_city_name(name, state, country_code=""):
    """Format city name with state (US/CA) or country code, respecting MAX_NAME_LEN."""
    if country_code.lower() in ("us", "ca") and state:
        candidate = f"{name}, {state}"
        if len(candidate) <= MAX_NAME_LEN:
            return candidate
        return _truncate_name(name)
    # All other countries: append 2-letter country code
    suffix = country_code.upper()
    candidate = f"{name}, {suffix}" if suffix else name
    if len(candidate) <= MAX_NAME_LEN:
        return candidate
    return _truncate_name(name)


# ---------------------------------------------------------------------------
# NPS parks
# ---------------------------------------------------------------------------

# In-memory list: [[name, lat, lon], ...]
_parks_db = []
_parks_loaded = False
_parks_lock = threading.Lock()


def _strip_park_name(full_name):
    """Remove common NPS suffixes for shorter display."""
    for suffix in _STRIP_SUFFIXES:
        if full_name.endswith(suffix):
            return full_name[: -len(suffix)]
    return full_name


def _download_parks():
    """Download all parks from NPS API (paginated, 50 per page)."""
    try:
        from config import NPS_API_KEY
    except (ImportError, ModuleNotFoundError, NameError):
        NPS_API_KEY = os.environ.get("NPS_API_KEY", "")

    if not NPS_API_KEY:
        logger.warning("[Landmarks] NPS_API_KEY not set — skipping parks download")
        return []

    logger.info("[Landmarks] Downloading NPS parks database...")
    parks = []
    start = 0
    limit = 50

    while True:
        try:
            r = requests.get(
                NPS_API_URL,
                params={"start": start, "limit": limit, "api_key": NPS_API_KEY},
                headers={"Accept": "application/json"},
                timeout=(10, 30),
            )
            r.raise_for_status()
            _log_api("nps")
            data = r.json()
        except Exception as e:
            logger.error(f"[Landmarks] NPS API error at offset {start}: {e}")
            break

        page_parks = data.get("data", [])
        if not page_parks:
            break

        for p in page_parks:
            try:
                lat_str = p.get("latitude", "")
                lon_str = p.get("longitude", "")
                if not lat_str or not lon_str:
                    continue
                lat = float(lat_str)
                lon = float(lon_str)
                name = _strip_park_name(p.get("fullName", p.get("name", "Unknown")))
                parks.append([name, lat, lon])
            except (ValueError, TypeError):
                continue

        total = int(data.get("total", "0"))
        start += limit
        if start >= total:
            break

    # Cache to disk
    try:
        with open(PARKS_CACHE, "w", encoding="utf-8") as f:
            json.dump({"parks": parks}, f)
        logger.info(f"[Landmarks] Cached {len(parks)} parks to nationalparks.json")
    except Exception as e:
        logger.error(f"[Landmarks] Cache write failed: {e}")

    return parks


def _load_parks():
    """Load parks from cache or download. Thread-safe.

    NOTE: deliberately does NOT call run_off_render_core(). This runs on the MAIN
    RENDER THREAD too — get_nearest_landmark() calls it synchronously, and that is
    reached from the tracked_stats keyframe (scenes/trackedstats.py) — so moving
    the caller's affinity here would un-pin the render thread from the isolated
    core, the exact inverse of the intent. The background entry point
    Overhead._preload_parks() makes the call before invoking this.
    """
    global _parks_db, _parks_loaded
    if _parks_loaded:
        return

    with _parks_lock:
        if _parks_loaded:
            return

        if os.path.exists(PARKS_CACHE):
            try:
                with open(PARKS_CACHE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                _parks_db = raw.get("parks", [])
                _parks_loaded = True
                logger.info(f"[Landmarks] Loaded {len(_parks_db)} parks from cache")
                return
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.error(f"[Landmarks] Cache corrupted, re-downloading: {e}")
            except Exception as e:
                logger.error(f"[Landmarks] Cache load failed: {e}")

        _parks_db = _download_parks()
        _parks_loaded = True


# ---------------------------------------------------------------------------
# Nominatim reverse geocoding (background thread)
# ---------------------------------------------------------------------------

_nom_city = None  # Last resolved settlement name (city/town/village only)
_nom_country = None  # Country name fallback from Nominatim response
_nom_query_lat = None  # Lat of the query that produced the result
_nom_query_lon = None  # Lon of the query that produced the result
_nom_fetching = False  # True while a background fetch is in flight
_nom_resolved = False  # A fetch COMPLETED for this area — see _over_water()
_nom_lock = threading.Lock()


def _nominatim_fetch(lat, lon):
    """Reverse-geocode via Nominatim. Runs in a background daemon thread.

    Stores settlement names and country names separately so that the main
    priority chain can prefer cities.json over vague country names.
    """
    global _nom_city, _nom_country, _nom_query_lat, _nom_query_lon, _nom_fetching
    global _nom_resolved
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json",
                    "zoom": 10, "addressdetails": 1},
            headers=_NOM_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        _log_api("nominatim")
        data = r.json()
        address = data.get("address", {})
        state = _get_state_abbr(address)
        country_code = address.get("country_code", "")

        # Try named settlement from address hierarchy (ASCII only)
        city_name = None
        for key in ("city", "town", "village", "municipality", "suburb"):
            candidate = address.get(key)
            if not candidate:
                continue
            # Reject non-ASCII names (unreadable on LED matrix)
            try:
                candidate.encode("ascii")
            except UnicodeEncodeError:
                continue
            # No length check here: _format_city_name truncates at a word
            # boundary. Skipping a long name instead silently DISCARDED the
            # correct settlement and fell through to the next address key, or
            # all the way to the cities.json nearest-neighbour -- which can be a
            # different town kilometres away. A long name is still the right
            # answer; it just needs trimming.
            city_name = _format_city_name(candidate, state, country_code)
            break

        # Country name stored separately — only used as last resort
        country_name = _country_name(country_code) if country_code else None

        with _nom_lock:
            _nom_city = city_name
            _nom_country = country_name
            _nom_query_lat = lat
            _nom_query_lon = lon
            _nom_resolved = True

    except Exception:
        # On any error, record the query coords so we don't retry immediately,
        # and DROP the water claim.
        #
        # _nom_resolved must not survive a failure. The water test is "a
        # completed lookup found no country", and an error completed nothing.
        # Leaving it set pins the last verdict onto every later position: a
        # flight that resolves mid-Atlantic (resolved=True, country=None) and
        # then crosses the Irish coast into a Nominatim outage keeps reporting
        # "North Atlantic" over County Kerry, re-confirming it every 15km for as
        # long as the outage lasts. Falling back to the nearest-city name is the
        # older, noisier behaviour, but it is merely imprecise rather than
        # confidently wrong about being at sea.
        with _nom_lock:
            _nom_query_lat = lat
            _nom_query_lon = lon
            _nom_resolved = False
    finally:
        with _nom_lock:
            _nom_fetching = False


def _over_water():
    """True only when a COMPLETED Nominatim lookup found no country at all.

    Nominatim answers open water with {"error": "Unable to geocode"} and land —
    however empty — with a country_code, so "resolved but no country" is a
    reliable water test.

    Distance to the nearest city is NOT such a test, which is why this exists.
    Measured against cities5000: remote LAND runs 280-760km from the nearest city
    (N Canada barrens 760, Australian outback 613, Greenland 512) while open
    WATER can be much closer (Bay of Biscay 155, mid-Pacific 222). The ranges
    overlap, so no distance threshold separates them.

    Gated on _nom_resolved because _nom_country is None both over water AND
    before the first background fetch returns; treating "not known yet" as water
    would flash an ocean name over land on every requery.

    Known and accepted: this classifies the CURRENT position using a lookup for a
    position up to REQUERY_AFTER_KM (15km) plus fetch latency behind it, so a
    coast crossing reports the wrong side for roughly the last 20km. Tightening
    that means querying Nominatim more often, which the render budget and their
    rate limit both argue against.
    """
    with _nom_lock:
        return _nom_resolved and _nom_country is None


def _ensure_nominatim(lat, lon):
    """
    Kick off a background Nominatim fetch if the plane has moved far enough.

    Returns (city_name, country_name) from the most recent Nominatim result.
    city_name is an actual settlement; country_name is a last-resort fallback.
    Either or both may be None if no result is available yet.
    """
    global _nom_fetching
    with _nom_lock:
        stale = (_nom_query_lat is None or
                 _haversine_km(lat, lon, _nom_query_lat, _nom_query_lon)
                 > REQUERY_AFTER_KM)
        if stale and not _nom_fetching:
            _nom_fetching = True
            try:
                threading.Thread(
                    target=_nominatim_fetch,
                    args=(lat, lon),
                    daemon=True,
                ).start()
            except Exception:
                _nom_fetching = False  # start() failed (OOM) — retry next call
        return _nom_city, _nom_country


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_nearest_landmark(latitude, longitude):
    """
    Find the nearest landmark (park, city, country, or ocean) to the given
    coordinates.

    Priority chain:
      1. NPS parks within 30km
      2. Nominatim settlement (actual city/town/village)
      3. Local cities.json nearest-neighbour
      4. Country name from Nominatim response
      5. Ocean/sea name from coordinate bounding boxes

    Steps 2 and 4 are separated so that cities.json (step 3) always beats
    a vague country name. "Topeka" is more useful than "United States".

    Step 3 is skipped over open water (see _over_water) — there the nearest city
    carries no information and the ocean name does.

    Returns {"name": str, "distance_km": float, "type": str} or None.
    """
    _load_parks()

    # 1. Check parks first (within radius)
    if _parks_db:
        best_park = None
        best_dist = float("inf")
        for name, plat, plon in _parks_db:
            dist = _haversine_km(latitude, longitude, plat, plon)
            if dist < best_dist:
                best_dist = dist
                best_park = name
        if best_park and best_dist <= PARKS_RADIUS_KM:
            # Parks bypassed MAX_NAME_LEN completely: 94 of the 474 NPS names
            # exceed it even after _strip_park_name, up to 65 chars
            # ("Washington-Rochambeau Revolutionary Route National Historic
            # Trail"), which holds the scroll far longer than any city name is
            # allowed to. Truncated here rather than at load time so the cache
            # stays faithful and a change to the cap takes effect immediately.
            return {"name": _truncate_name(best_park),
                    "distance_km": best_dist, "type": "park"}

    # 2. Nominatim settlement only (not country/ocean fallbacks)
    nom_city, nom_country = _ensure_nominatim(latitude, longitude)
    if nom_city:
        return {"name": nom_city, "distance_km": 0.0, "type": "city"}

    # 3. Fall back to local cities.json nearest-neighbour
    city = get_nearest_city(latitude, longitude)

    # 3a. Over open water the nearest city is not a location, it is noise: a plane
    #     mid-Atlantic was labelled "nr Ribeira Grande" for a town 1079km away,
    #     rendered identically to a town it was actually over. Name the water.
    #     Only fires on a resolved no-country lookup, so land is untouched.
    if _over_water():
        return {"name": _water_name(latitude, longitude),
                "distance_km": 0.0, "type": "ocean"}

    if city:
        return {
            "name": _format_city_name(
                city["name"],
                _state_from_admin1(city.get("country"), city.get("admin1")),
                city.get("country") or ""),
            "distance_km": city["distance_km"],
            "type": "city",
        }

    # 4. Country name from Nominatim response (last resort for land)
    if nom_country:
        return {"name": nom_country, "distance_km": 0.0, "type": "country"}

    # 5. Ocean/sea name from bounding boxes (last resort for water)
    ocean = _get_ocean_name(latitude, longitude)
    if ocean:
        return {"name": ocean, "distance_km": 0.0, "type": "ocean"}

    return None


def clear_cache():
    """Clear Nominatim cache — forces re-query on next call."""
    global _nom_city, _nom_country, _nom_query_lat, _nom_query_lon, _nom_fetching
    global _nom_resolved
    with _nom_lock:
        _nom_city = None
        _nom_country = None
        _nom_query_lat = None
        _nom_query_lon = None
        _nom_fetching = False
        _nom_resolved = False


def preload():
    """Background preload — call from main thread at startup."""
    t = threading.Thread(target=_load_parks, daemon=True)
    t.start()
