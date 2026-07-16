"""
air_quality.py — current US EPA AQI for the home location.

PRIMARY: AirNow's reporting-area endpoint — the same one airnow.gov itself
calls. It returns the official OBSERVED AQI for your EPA *reporting area*
(e.g. "Long Island Region"), which is the number your local health department
and the news quote. Keyless.

FALLBACK: Open-Meteo's modelled `us_aqi`, used only if AirNow is unreachable or
has no data for the location. Modelled data tracks the trend but can read well
low during smoke events, so it is a safety net, not the first choice.

Why not airnowapi.org (the *documented* AirNow API)? It resolves to the nearest
individual MONITOR, not a reporting area. For coastal/rural locations that is
often another region's ozone-only station — it reported AQI 18 (ozone, from
across a 25-mile sound) on a day the reporting area was 93 on PM2.5 — and it
returns nothing at all for many ZIP codes. The reporting-area endpoint returns
what the website shows.

The reporting-area lookup needs a 2-letter stateCode: without it the endpoint
falls back to nearest-monitor behaviour and picks the wrong region. The home
state is reverse-geocoded once via Nominatim and cached to disk (home doesn't
move); if that can't be resolved, we use the Open-Meteo fallback.

No API key required. Off unless AQI_ALERTS_ENABLED.

Usage:
    from utilities.air_quality import get_aqi
    aqi = get_aqi()   # int 0-500, or None
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

_AIRNOW_URL = "https://airnowgovapi.com/reportingarea/get"
_OPENMETEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_UA = "plane-tracker-rgb-pi (air-quality chip)"

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "air_quality.json")
_STATE_FILE = os.path.join(_CACHE_DIR, "aqi_state.json")
_POLL_INTERVAL = 1800  # 30 min — the hourly AQI doesn't move faster
_MAX_AGE = 3 * 3600    # a value not refreshed within this is stale -> not shown

_cached_aqi = None   # last good AQI (int) or None
_cached_ts = 0.0     # last fetch-ATTEMPT timestamp (gates the poll interval)
_value_ts = 0.0      # when _cached_aqi was last SUCCESSFULLY fetched (freshness)
_state = None        # 2-letter home state code, or "" once resolution has failed


def _load_cache():
    """Seed the in-memory value from disk once (survives a restart)."""
    try:
        import json
        with open(_CACHE_FILE) as f:
            d = json.load(f)
        return d.get("aqi"), float(d.get("ts", 0))
    except Exception:
        return None, 0.0


def _home_state(loc):
    """2-letter state code for the home location, or "" if unresolvable.

    Reverse-geocoded once and cached to disk — the home location doesn't move,
    so this is a one-time cost. Nominatim asks callers to be light on it; we
    hit it at most once per install.
    """
    global _state
    if _state is not None:
        return _state
    import json
    try:                                  # disk cache (survives restarts)
        with open(_STATE_FILE) as f:
            d = json.load(f)
        if d.get("lat") == loc[0] and d.get("lon") == loc[1]:
            _state = d.get("state", "") or ""
            return _state
    except Exception:
        pass
    _state = ""
    try:
        # zoom=10 (locality) rather than 5 (state): border locations like a town
        # right across a river resolve to the correct state, and the response
        # still carries the state ISO code.
        r = requests.get(_NOMINATIM_URL, params={
            "lat": loc[0], "lon": loc[1], "format": "json", "zoom": 10,
        }, headers={"User-Agent": _UA}, timeout=(4, 8))
        r.raise_for_status()
        addr = (r.json() or {}).get("address") or {}
        iso = addr.get("ISO3166-2-lvl4", "")   # e.g. "US-NY"
        if iso and "-" in iso:
            _state = iso.split("-")[-1]
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = f"{_STATE_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"lat": loc[0], "lon": loc[1], "state": _state}, f)
        os.replace(tmp, _STATE_FILE)
        logger.info("[AQI] home state resolved: %s", _state or "(unknown)")
    except Exception as e:
        logger.error(f"[AQI] state lookup failed: {e}")
    return _state


def _fetch_airnow(loc, state):
    """Official observed AQI for the reporting area, or None."""
    r = requests.post(_AIRNOW_URL, data={
        "latitude": loc[0],
        "longitude": loc[1],
        "stateCode": state,
        "maxDistance": 50,
    }, headers={"User-Agent": _UA}, timeout=(4, 8))
    r.raise_for_status()
    # dataType "O" = observed, "F" = forecast. We want the current observation,
    # and the overall AQI is the max across the reported pollutants.
    aqis = [x.get("aqi") for x in (r.json() or [])
            if isinstance(x, dict) and x.get("dataType") == "O"
            and isinstance(x.get("aqi"), (int, float))
            and not isinstance(x.get("aqi"), bool) and x.get("aqi") >= 0]
    return int(round(max(aqis))) if aqis else None


def _fetch_openmeteo(loc):
    """Modelled us_aqi (already the max across sub-indices), or None."""
    r = requests.get(_OPENMETEO_URL, params={
        "latitude": loc[0], "longitude": loc[1], "current": "us_aqi",
    }, timeout=(4, 8))
    r.raise_for_status()
    val = (r.json().get("current") or {}).get("us_aqi")
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
        return int(round(val))
    return None


def get_aqi():
    """Current US EPA AQI (int 0-500) for the home location, or None.

    Config is read at CALL TIME (so enabling it in the web config page takes
    effect without a restart). Gated to one poll per _POLL_INTERVAL, and the
    timestamp is advanced BEFORE the blocking calls so an unreachable source
    can't drive a request every second on the 1 Hz render thread. On failure the
    last good value is kept, until it ages past _MAX_AGE.
    """
    global _cached_aqi, _cached_ts, _value_ts

    import config as cfg
    if not getattr(cfg, "AQI_ALERTS_ENABLED", False):
        return None
    loc = getattr(cfg, "LOCATION_HOME", None) or [0.0, 0.0]
    if loc == [0.0, 0.0]:
        return None

    now = time.time()
    # Seed from disk on a cold start so a fresh process shows a value fast —
    # but only if the cached reading is still fresh, so a restart during an
    # outage can't resurrect an arbitrarily-old AQI and pin it on screen.
    if _cached_aqi is None and _cached_ts == 0.0:
        aqi0, ts0 = _load_cache()
        if aqi0 is not None and (now - ts0) < _MAX_AGE:
            _cached_aqi, _value_ts = aqi0, ts0
    fresh = _cached_aqi is not None and (now - _value_ts) < _MAX_AGE
    if (now - _cached_ts) < _POLL_INTERVAL:
        return _cached_aqi if fresh else None
    _cached_ts = now   # advance BEFORE the blocking calls (anti-hammer)

    val, src = None, None
    state = _home_state(loc)
    if state:
        try:
            val = _fetch_airnow(loc, state)
            src = "AirNow %s" % state
        except Exception as e:
            logger.error(f"[AQI] AirNow fetch failed: {e}")
    if val is None:                        # unreachable, no data, or no state
        try:
            val = _fetch_openmeteo(loc)
            src = "Open-Meteo"
        except Exception as e:
            logger.error(f"[AQI] Open-Meteo fetch failed: {e}")

    if val is not None:
        _cached_aqi = val
        _value_ts = now   # mark this value fresh (last successful fetch)
        try:
            import json
            os.makedirs(_CACHE_DIR, exist_ok=True)
            tmp = f"{_CACHE_FILE}.tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"aqi": _cached_aqi, "ts": now}, f)
            os.replace(tmp, _CACHE_FILE)
        except Exception as e:
            logger.error(f"[AQI] cache write failed: {e}")
        logger.info("[AQI] %s (%s)", _cached_aqi, src)

    # A value we haven't managed to refresh within _MAX_AGE is stale — hide it
    # rather than show a wrong reading indefinitely while fetches keep failing.
    return _cached_aqi if (_cached_aqi is not None and (now - _value_ts) < _MAX_AGE) else None
