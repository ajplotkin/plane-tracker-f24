"""
air_quality.py — current AQI from the EPA AirNow API.

Returns the overall US EPA Air Quality Index (0-500) for the home location —
the max across the reporting pollutants (O3 / PM2.5 / PM10) from the nearest
monitoring stations. Used to draw a small colour-coded "A<nnn>" chip next to the
clock when the AQI is at/above a configurable threshold.

Free API key from https://docs.airnowapi.org/. Off unless AQI_ALERTS_ENABLED and
AIRNOW_API_KEY are set.

Usage:
    from utilities.air_quality import get_aqi
    aqi = get_aqi()   # int 0-500, or None
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.airnowapi.org/aq/observation/latLong/current/"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "air_quality.json")
_POLL_INTERVAL = 1800  # 30 min — AirNow observations update ~hourly
_MAX_AGE = 3 * 3600    # a value not refreshed within this is stale -> not shown

_cached_aqi = None   # last good AQI (int) or None
_cached_ts = 0.0     # last fetch-ATTEMPT timestamp (gates the poll interval)
_value_ts = 0.0      # when _cached_aqi was last SUCCESSFULLY fetched (freshness)


def _load_cache():
    """Seed the in-memory value from disk once (survives a restart)."""
    try:
        import json
        with open(_CACHE_FILE) as f:
            d = json.load(f)
        return d.get("aqi"), float(d.get("ts", 0))
    except Exception:
        return None, 0.0


def get_aqi():
    """Current EPA AQI (int 0-500) for the home location, or None.

    Config is read at CALL TIME (so enabling it / entering the key in the web
    config page takes effect without a restart). Gated to one AirNow call per
    _POLL_INTERVAL, and the timestamp is advanced BEFORE the blocking GET so an
    unreachable AirNow / bad key can't drive a request every second on the 1 Hz
    render thread. On failure the last good value is kept.
    """
    global _cached_aqi, _cached_ts, _value_ts

    import config as cfg
    if not getattr(cfg, "AQI_ALERTS_ENABLED", False):
        return None
    key = getattr(cfg, "AIRNOW_API_KEY", "") or ""
    loc = getattr(cfg, "LOCATION_HOME", None) or [0.0, 0.0]
    if not key or loc == [0.0, 0.0]:
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
    _cached_ts = now   # advance BEFORE the blocking call (anti-hammer)

    try:
        r = requests.get(_BASE_URL, params={
            "format": "application/json",
            "latitude": loc[0],
            "longitude": loc[1],
            "distance": 50,   # miles — nearest reporting area
            "API_KEY": key,
        }, timeout=(4, 8))
        r.raise_for_status()
        obs = r.json()
        # One entry per pollutant; the reported AQI is the max across them.
        aqis = [o["AQI"] for o in obs
                if isinstance(o, dict) and isinstance(o.get("AQI"), int) and o["AQI"] >= 0]
        if aqis:
            _cached_aqi = max(aqis)
            _value_ts = now   # mark this value fresh (last successful fetch)
            os.makedirs(_CACHE_DIR, exist_ok=True)
            import json
            tmp = f"{_CACHE_FILE}.tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"aqi": _cached_aqi, "ts": now}, f)
            os.replace(tmp, _CACHE_FILE)
            logger.info("[AQI] %s (AirNow)", _cached_aqi)
    except Exception as e:
        logger.error(f"[AQI] fetch failed: {e}")
    # A value we haven't managed to refresh within _MAX_AGE is stale — hide it
    # rather than show a wrong reading indefinitely while fetches keep failing.
    return _cached_aqi if (_cached_aqi is not None and (now - _value_ts) < _MAX_AGE) else None
