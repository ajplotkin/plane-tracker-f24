"""
airlabs.py — Flight schedule lookup via AirLabs API.

Used to get departure/arrival info for flights that haven't taken off yet.
The FR24 gRPC feed only shows airborne flights — AirLabs fills the gap
for pre-departure schedule data.

Free tier: 1000 credits/month, 1 credit per /schedules call.
API key set via AIRLABS_API_KEY env var or config.

Usage:
    from utilities.airlabs import get_flight_schedule
    sched = get_flight_schedule("UA353")
    # {"origin": "EWR", "destination": "LAX", "dep_time": "2026-05-11 18:30", ...}
"""

import logging
import os
from datetime import datetime, timedelta
from time import time

import requests

try:
    from utilities.api_usage import log_call as _log_api
except ImportError:
    _log_api = lambda source: None

logger = logging.getLogger(__name__)

_API_BASE = "https://airlabs.co/api/v9"

# Module-level cache: callsign -> (result, timestamp)
# Prevents repeated API calls for the same flight (web UI + overhead.py)
_cache = {}
_CACHE_TTL = 300  # 5 minutes

# Try config first, fall back to env var
try:
    from config import AIRLABS_API_KEY
except (ImportError, ModuleNotFoundError, NameError):
    AIRLABS_API_KEY = None

if not AIRLABS_API_KEY:
    AIRLABS_API_KEY = os.environ.get("AIRLABS_API_KEY", "")


def get_flight_schedule(callsign):
    """
    Look up flight schedule from AirLabs.

    Accepts IATA (UA353) or ICAO (UAL353) format.
    Returns the next upcoming segment for this flight number, or None.

    Returns:
        {
            "origin": "EWR",
            "destination": "LAX",
            "dep_time": "2026-05-11 18:30",
            "dep_time_utc": "2026-05-11 22:30",
            "arr_time": "2026-05-11 21:45",
            "arr_time_utc": "2026-05-12 01:45",
            "status": "scheduled",
            "airline_iata": "UA",
            "flight_number": "UA353",
            "duration": 330,
        }
        or None on error / not found.
    """
    if not AIRLABS_API_KEY:
        logger.warning("AirLabs: No API key configured")
        return None

    callsign = callsign.strip().upper()
    if not callsign:
        return None

    # Evict expired entries periodically
    now_ts = time()
    if len(_cache) > 200:
        expired = [k for k, (_, ts) in _cache.items() if now_ts - ts >= _CACHE_TTL]
        for k in expired:
            del _cache[k]

    # Check module-level cache first
    cached = _cache.get(callsign)
    if cached and (now_ts - cached[1]) < _CACHE_TTL:
        return cached[0]

    # Determine if IATA (2-letter + digits) or ICAO (3-letter + digits)
    params = {"api_key": AIRLABS_API_KEY}
    if len(callsign) >= 4 and callsign[:3].isalpha() and callsign[3:].isdigit():
        params["flight_icao"] = callsign
    else:
        params["flight_iata"] = callsign

    try:
        logger.info(f"AirLabs: Looking up schedule for {callsign}")
        r = requests.get(f"{_API_BASE}/schedules", params=params, timeout=(5, 15))
        r.raise_for_status()
        _log_api("airlabs")
        data = r.json()

        schedules = data.get("response", [])
        if not schedules:
            logger.info(f"AirLabs: No schedule found for {callsign}")
            _cache[callsign] = (None, time())
            return None

        now = time()

        def _arr_ts(s):
            """Best-effort arrival unix ts: explicit field, else dep+duration."""
            a = s.get("arr_time_ts")
            if a:
                return a
            d = s.get("dep_time_ts")
            dur = s.get("duration")   # minutes
            if d and dur:
                try:
                    return d + int(dur) * 60
                except (TypeError, ValueError):
                    return d
            return d

        # Keep legs whose ARRIVAL is still in the future (currently airborne or
        # upcoming), with a 2h grace so a just-landed leg lingers long enough
        # for the caller's "+2h past arrival -> wipe" logic to fire. Filtering
        # on DEPARTURE within the last hour dropped the current leg of any
        # flight airborne >1h and returned TOMORROW's leg, whose future arrival
        # defeated that wipe (a dead SCHEDULED flight stayed up to the 36h cap).
        upcoming = [s for s in schedules
                    if _arr_ts(s) and _arr_ts(s) > now - 7200]
        if not upcoming:
            upcoming = schedules

        # Among the still-relevant legs, pick the one DEPARTING soonest: an
        # in-air leg has a past dep_time_ts (sorts first, so it's chosen over a
        # later connecting/next-day leg), and among future legs the next to
        # depart wins.
        upcoming.sort(key=lambda s: s.get("dep_time_ts", 0))
        best = upcoming[0]

        # Single construction path — see _build_result. This used to be an
        # inline duplicate of that dict, so any field added to one silently
        # missed the other.
        result = _build_result(best, callsign)
        logger.info(f"AirLabs: Found {result['flight_number']} {result['origin']}→{result['destination']} status={result['status']}")
        _cache[callsign] = (result, time())
        return result

    except requests.exceptions.Timeout:
        logger.warning("AirLabs: Request timed out")
        _cache[callsign] = (None, time())
        return None
    except Exception as e:
        logger.warning(f"AirLabs: Error looking up {callsign}: {e}")


def get_flight_legs(callsign):
    """Return all upcoming legs for a flight number (for multi-leg picker).
    Uses get_flight_schedule to fetch data (single API call), then re-parses
    the raw response for multiple legs. Returns list of schedule dicts."""
    callsign = callsign.strip().upper()
    if not callsign or not AIRLABS_API_KEY:
        return []

    # Check module cache first
    now_ts = time()
    cached = _cache.get(callsign)
    if cached and (now_ts - cached[1]) < _CACHE_TTL:
        # Cache only has the single best leg; need raw response for multi-leg
        pass

    params = {"api_key": AIRLABS_API_KEY}
    if len(callsign) >= 4 and callsign[:3].isalpha() and callsign[3:].isdigit():
        params["flight_icao"] = callsign
    else:
        params["flight_iata"] = callsign

    try:
        r = requests.get(f"{_API_BASE}/schedules", params=params, timeout=(5, 15))
        r.raise_for_status()
        _log_api("airlabs")
        schedules = r.json().get("response", [])
        now = time()
        upcoming = [
            s for s in schedules
            if s.get("dep_time_ts") and s["dep_time_ts"] > now - 3600
        ]
        if not upcoming:
            upcoming = schedules
        upcoming.sort(key=lambda s: s.get("dep_time_ts", 0))
        legs = []
        for s in upcoming:
            legs.append({
                "origin": s.get("dep_iata", ""),
                "destination": s.get("arr_iata", ""),
                "dep_time": s.get("dep_time", ""),
                "dep_time_utc": s.get("dep_time_utc", ""),
                "dep_time_ts": s.get("dep_time_ts"),
                "arr_time": s.get("arr_time", ""),
                "arr_time_utc": s.get("arr_time_utc", ""),
                "status": s.get("status", ""),
                "airline_iata": s.get("airline_iata", ""),
                "flight_number": s.get("flight_iata", callsign),
                "cs_airline_iata": s.get("cs_airline_iata", ""),
                "duration": s.get("duration"),
            })
        # Also update module cache with best leg for get_flight_schedule
        if legs:
            _cache[callsign] = (_build_result(upcoming[0], callsign), time())
        return legs
    except Exception as e:
        logger.warning(f"AirLabs: Error in get_flight_legs: {e}")
        # Fall back to single-leg from get_flight_schedule
        result = get_flight_schedule(callsign)
        return [result] if result else []


def _build_result(best, callsign):
    """Build a schedule result dict from a raw AirLabs schedule entry."""
    return {
        "origin": best.get("dep_iata", ""),
        "destination": best.get("arr_iata", ""),
        "dep_time": best.get("dep_time", ""),
        "dep_time_utc": best.get("dep_time_utc", ""),
        "arr_time": best.get("arr_time", ""),
        "arr_time_utc": best.get("arr_time_utc", ""),
        "arr_estimated_utc": best.get("arr_estimated_utc", ""),
        "arr_actual_utc": best.get("arr_actual_utc", ""),
        # --- Departure-side revisions (see departure_delay) ---
        "dep_estimated": best.get("dep_estimated", ""),
        "dep_estimated_utc": best.get("dep_estimated_utc", ""),
        "dep_estimated_ts": best.get("dep_estimated_ts"),
        "dep_actual": best.get("dep_actual", ""),
        "dep_actual_utc": best.get("dep_actual_utc", ""),
        "dep_actual_ts": best.get("dep_actual_ts"),
        "dep_delayed": best.get("dep_delayed"),      # minutes
        "delayed": best.get("delayed"),              # minutes (deprecated by AirLabs)
        "status": best.get("status", ""),
        "airline_iata": best.get("airline_iata", ""),
        "airline_icao": best.get("airline_icao", ""),
        "flight_number": best.get("flight_iata", callsign),
        "flight_icao": best.get("flight_icao", ""),
        "cs_airline_iata": best.get("cs_airline_iata", ""),  # Operating carrier IATA (e.g., YX=Republic)
        "dep_time_ts": best.get("dep_time_ts"),              # Scheduled departure unix timestamp
        "duration": best.get("duration"),
    }


# --- Departure delay derivation ------------------------------------------
#
# AirLabs /schedules documents these departure-side fields (verified against
# https://airlabs.co/docs/schedules):
#
#     dep_estimated / dep_estimated_ts / dep_estimated_utc  updated dep time
#     dep_actual    / dep_actual_ts    / dep_actual_utc     actual dep time
#     dep_delayed                                           dep delay, minutes
#     delayed                                               flight delay, minutes
#                                                           (marked deprecated)
#
# CAVEAT — FREE TIER. The docs annotate only a subset of fields "Available in
# the Free plan" (airline_iata, flight_iata, flight_number, dep_iata, dep_time,
# arr_iata, arr_time). NONE of the delay fields above carry that annotation, so
# on the free key this tracker uses they may simply be absent from the payload.
# Every field is therefore treated as optional and each source is tried in turn;
# when they are all missing the result is (None, "") — i.e. exactly the
# pre-delay behaviour, no invented numbers.

_TIME_FMT = "%Y-%m-%d %H:%M"


def _parse_time(value):
    """Parse an AirLabs 'YYYY-MM-DD HH:MM' string into a naive datetime, or None."""
    try:
        return datetime.strptime(value, _TIME_FMT)
    except (TypeError, ValueError):
        return None


def _shift_time(value, minutes):
    """Return `value` ('YYYY-MM-DD HH:MM') moved forward by `minutes`, or ''."""
    dt = _parse_time(value)
    if dt is None:
        return ""
    return (dt + timedelta(minutes=minutes)).strftime(_TIME_FMT)


def _minutes_between(rev_ts, base_ts, rev_utc, base_utc, rev_local, base_local):
    """Minutes from base to revised, using whichever pair AirLabs actually sent.

    Unix timestamps first (unambiguous), then the UTC strings, then the
    airport-local strings as a last resort — local strings are compared as
    naive datetimes, which would misreport across a DST change, hence last.
    Returns None when no usable pair exists.
    """
    if rev_ts and base_ts:
        try:
            return int((float(rev_ts) - float(base_ts)) // 60)
        except (TypeError, ValueError):
            pass
    for rev, base in ((rev_utc, base_utc), (rev_local, base_local)):
        rev_dt, base_dt = _parse_time(rev or ""), _parse_time(base or "")
        if rev_dt and base_dt:
            return int((rev_dt - base_dt).total_seconds() // 60)
    return None


def _as_minutes(value):
    """Coerce an AirLabs delay field to int minutes, or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def departure_delay(sched):
    """
    Derive the departure delay for a schedule dict from get_flight_schedule.

    Returns (delay_minutes, revised_dep_time):
      delay_minutes    int >= 0 when AirLabs told us something (0 == known
                       on-time or early), or None when it told us nothing.
                       None and 0 are deliberately distinct: None means
                       "unknown", so callers show the scheduled time unadorned.
      revised_dep_time the revised departure in the DEPARTURE AIRPORT's local
                       time, formatted like dep_time ('2026-05-11 20:15'),
                       or '' when there is nothing to revise.

    An early departure is reported as 0 rather than a negative number: the
    64x32 panel has no room to explain "-10m", and nobody misses a flight for
    leaving late-listed but early.
    """
    if not sched:
        return None, ""

    sched_local = sched.get("dep_time") or ""

    # 1/2. An explicit revised departure time gives both the delay and the
    #      exact time to display. Actual beats estimated.
    for kind in ("actual", "estimated"):
        delta = _minutes_between(
            sched.get(f"dep_{kind}_ts"), sched.get("dep_time_ts"),
            sched.get(f"dep_{kind}_utc"), sched.get("dep_time_utc"),
            sched.get(f"dep_{kind}"), sched_local,
        )
        if delta is None:
            continue
        revised = (sched.get(f"dep_{kind}") or "") or _shift_time(sched_local, delta)
        return max(delta, 0), revised

    # 3. Only a delay duration — derive the revised time from the schedule.
    #
    # ONLY dep_delayed. NOT `delayed`: despite the name it is the ARRIVAL delay.
    # Verified against a live /schedules call for EWR (2026-08-17, 100 rows) — in
    # every row `delayed` equalled `arr_delayed`, never the departure figure:
    #
    #   flight   sched  actual  dep_delayed  arr_delayed  delayed   actual-sched
    #   UA350    17:50  17:39   None         11           11        -11 (EARLY)
    #   UA1202   17:55  17:50   None         40           40         -5 (EARLY)
    #   NZ9123   17:00  17:48   48           81           81        +48
    #
    # Falling through to it would announce "+40" for a flight leaving 5 minutes
    # EARLY. (The docs also mark `delayed` deprecated.)
    for key in ("dep_delayed",):
        minutes = _as_minutes(sched.get(key))
        if minutes is None:
            continue
        if minutes <= 0:
            return 0, sched_local
        return minutes, _shift_time(sched_local, minutes)

    return None, ""
