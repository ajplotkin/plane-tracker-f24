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

LEG PINNING
-----------
A flight NUMBER is not a flight. UA353 flies every day, and often two or three
legs in one day, so /schedules returns several rows and this module has to pick
one. Picking "the soonest-departing still-relevant leg" (the unpinned path
below) is right while a flight is being watched TOWARDS departure, but it is
wrong for deciding that a tracked flight is OVER: once today's leg ages out of
the selection window, TOMORROW's leg is substituted, the arrival jumps a day
into the future, and overhead.py's "arrival + 2h has passed -> wipe" rule can
never fire again (the two windows were exact negations of each other).

So a caller that is tracking ONE leg passes `pin_dep_ts` — the scheduled
departure it saved when the flight was chosen — and gets back the leg NEAREST
that time, or `LEG_GONE`. Nearest-match, not window-match: same-number legs on
one day are 2-3h apart and must still resolve, while the next day's leg is 24h
away and is excluded by `_PIN_TOLERANCE_SEC` with room to spare. The pin fixes
the leg's IDENTITY only — its TIMES are re-read on every fetch, so departure
delays are still picked up.
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

# Module-level cache: cache key -> (result, timestamp)
# Prevents repeated API calls for the same flight (web UI + overhead.py).
# The key carries the pin (see _cache_key): a pinned lookup and an unpinned one
# for the same callsign are different questions and must not share an answer.
_cache = {}
_CACHE_TTL = 300  # 5 minutes

# How far a leg's scheduled departure may sit from the pin and still be "it".
# Comfortably wider than a same-day leg gap (2-3h) is NOT the goal — nearest
# wins outright — this is only the sanity bound that separates "my leg, retimed"
# from "my leg is gone and you are looking at a different day".
_PIN_TOLERANCE_SEC = 7200   # 2 hours


class _LegGone:
    """Sentinel: the pinned leg is absent from an otherwise VALID response.

    Deliberately falsy, so any caller that only does `if sched:` treats it as
    "no data" — the safe default. A caller that must tell "the leg is over /
    gone" apart from "AirLabs did not answer" (which returns None) tests
    `result is LEG_GONE` FIRST. That distinction is the whole point: only the
    former may wipe a tracked flight; the latter must keep showing it.
    """
    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return "LEG_GONE"


LEG_GONE = _LegGone()


def _cache_key(callsign, pin_dep_ts):
    return callsign if pin_dep_ts is None else (callsign, int(pin_dep_ts))


def _nearest_leg(schedules, pin_dep_ts, tolerance=_PIN_TOLERANCE_SEC):
    """The leg whose SCHEDULED departure is nearest `pin_dep_ts`, or None.

    Matched on dep_time_ts (the scheduled time), never on dep_estimated/actual:
    a delayed leg keeps its scheduled departure, so pinning on it survives a
    delay — which is exactly what the departure-delay feature needs.
    """
    best, best_delta = None, None
    for s in schedules or []:
        dep = s.get("dep_time_ts")
        if not dep:
            continue
        try:
            delta = abs(float(dep) - pin_dep_ts)
        except (TypeError, ValueError):
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = s, delta
    if best is None or best_delta > tolerance:
        return None
    return best

# Try config first, fall back to env var
try:
    from config import AIRLABS_API_KEY
except (ImportError, ModuleNotFoundError, NameError):
    AIRLABS_API_KEY = None

if not AIRLABS_API_KEY:
    AIRLABS_API_KEY = os.environ.get("AIRLABS_API_KEY", "")


def get_pinned_schedule(callsign, dep_ts):
    """The leg of `callsign` scheduled to depart at `dep_ts` — see LEG PINNING.

    Returns the schedule dict, LEG_GONE (valid response, leg not in it), or
    None (AirLabs unreachable / no key). A thin alias for the pinned form of
    get_flight_schedule; exists so call sites read as what they mean.
    """
    return get_flight_schedule(callsign, pin_dep_ts=dep_ts)


def get_flight_schedule(callsign, pin_dep_ts=None):
    """
    Look up flight schedule from AirLabs.

    Accepts IATA (UA353) or ICAO (UAL353) format.
    Returns the next upcoming segment for this flight number, or None.

    `pin_dep_ts` (unix ts) pins the lookup to ONE leg — the one departing
    nearest that time (see LEG PINNING in the module docstring). With a pin the
    return is the schedule dict, LEG_GONE when the response is valid but holds
    no leg within `_PIN_TOLERANCE_SEC`, or None on an API failure. Without a
    pin the behaviour is unchanged: soonest-departing still-relevant leg, or
    None for both "nothing found" and "API failure".

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

    # An unusable pin degrades to the unpinned behaviour rather than throwing
    # away every leg: a garbage pin must not look like "the leg is gone".
    if pin_dep_ts is not None:
        try:
            pin_dep_ts = float(pin_dep_ts)
        except (TypeError, ValueError):
            logger.warning(f"AirLabs: ignoring unusable pin {pin_dep_ts!r} for {callsign}")
            pin_dep_ts = None

    # Evict expired entries periodically
    now_ts = time()
    if len(_cache) > 200:
        expired = [k for k, (_, ts) in _cache.items() if now_ts - ts >= _CACHE_TTL]
        for k in expired:
            del _cache[k]

    # Check module-level cache first
    key = _cache_key(callsign, pin_dep_ts)
    cached = _cache.get(key)
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
        if not schedules and pin_dep_ts is None:
            logger.info(f"AirLabs: No schedule found for {callsign}")
            _cache[key] = (None, time())
            return None

        if pin_dep_ts is not None:
            # PINNED: this caller is tracking one specific leg. An empty
            # response counts as "gone" — the request succeeded and the leg is
            # simply no longer scheduled, which is what a finished flight looks
            # like a few hours later.
            best = _nearest_leg(schedules, pin_dep_ts)
            if best is None:
                logger.info(
                    f"AirLabs: pinned leg for {callsign} (dep_ts={int(pin_dep_ts)}) "
                    f"not in response ({len(schedules)} leg(s)) — LEG_GONE")
                _cache[key] = (LEG_GONE, time())
                return LEG_GONE
            result = _build_result(best, callsign)
            logger.info(
                f"AirLabs: pinned {result['flight_number']} "
                f"{result['origin']}→{result['destination']} status={result['status']}")
            _cache[key] = (result, time())
            return result

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
        _cache[key] = (result, time())
        return result

    except requests.exceptions.Timeout:
        logger.warning("AirLabs: Request timed out")
        # A timeout is an API FAILURE, never "the leg is gone" — caching None
        # under a pinned key is right: None keeps the caller's cached leg.
        _cache[key] = (None, time())
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
        # Unix arrival, when AirLabs sends it — unambiguous where the UTC
        # strings need parsing. overhead.best_arrival_ts falls back to it.
        "arr_time_ts": best.get("arr_time_ts"),
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
