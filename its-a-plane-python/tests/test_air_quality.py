"""AQI fetcher (AirNow reporting-area primary, Open-Meteo fallback) + EPA bands.

rgbmatrix is stubbed in tests/conftest.py (graphics.Color is a REAL class there,
so the colour-band identity assertions below are meaningful).
"""
import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import config as cfg
from utilities import air_quality

# Redirect the on-disk caches to a scratch dir so tests can't write phantom AQI
# data into the real .cache/ that the display + web processes read.
_TMP = tempfile.mkdtemp(prefix="aqi_test_")
air_quality._CACHE_DIR = _TMP
air_quality._CACHE_FILE = os.path.join(_TMP, "air_quality.json")
air_quality._STATE_FILE = os.path.join(_TMP, "aqi_state.json")


def _reset(state="CO"):
    air_quality._cached_aqi = None
    air_quality._cached_ts = 1.0    # non-zero -> skip the cold-start disk seed
    air_quality._value_ts = 0.0
    air_quality._state = state      # pre-resolve so tests don't hit Nominatim
    air_quality._state_loc = None   # pinned to the loc by _set_cfg below
    air_quality._state_try_ts = 0.0
    for f in (air_quality._CACHE_FILE, air_quality._STATE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass


def _set_cfg(enabled=True, loc=None):
    loc = loc if loc is not None else [39.74, -104.99]   # Denver — synthetic
    cfg.AQI_ALERTS_ENABLED = enabled
    cfg.LOCATION_HOME = loc
    # Pin the pre-resolved state to THIS loc so _home_state's memo hits and the
    # tests never reach Nominatim.
    air_quality._state_loc = list(loc)


def _airnow(entries):
    r = MagicMock()
    r.json.return_value = entries
    r.raise_for_status = lambda: None
    return r


def _om(us_aqi):
    r = MagicMock()
    r.json.return_value = {"current": {"us_aqi": us_aqi}}
    r.raise_for_status = lambda: None
    return r


# ── AirNow primary ──────────────────────────────────────────────────────────

def test_airnow_returns_max_observed_and_ignores_forecast():
    """Real shape: observed PM2.5 93 + ozone 19, forecast PM2.5 185.
    Must report 93 (max of OBSERVED), never the 185 forecast."""
    _reset(); _set_cfg()
    entries = [
        # OZONE deliberately FIRST and PM10 last: a `aqis[0]` implementation must
        # fail this. Real responses order pollutants arbitrarily.
        {"reportingArea": "Example Region", "parameter": "OZONE", "aqi": 19, "dataType": "O"},
        {"reportingArea": "Example Region", "parameter": "PM2.5", "aqi": 93, "dataType": "O"},
        {"reportingArea": "Example Region", "parameter": "PM10", "aqi": 36, "dataType": "O"},
        {"reportingArea": "Example Region", "parameter": "PM2.5", "aqi": 185, "dataType": "F"},
        {"reportingArea": "Example Region", "parameter": "OZONE", "aqi": None, "dataType": "F"},
    ]
    with patch("utilities.air_quality.requests.post", return_value=_airnow(entries)) as p, \
         patch("utilities.air_quality.requests.get") as g:
        assert air_quality.get_aqi() == 93
        assert p.call_args.kwargs["data"]["stateCode"] == "CO"   # the discriminator
        g.assert_not_called()                                    # no fallback needed


def test_airnow_state_code_is_sent():
    """Without stateCode the endpoint silently returns the wrong region."""
    _reset(state="WA"); _set_cfg(loc=[47.61, -122.33])   # Seattle — synthetic
    with patch("utilities.air_quality.requests.post",
               return_value=_airnow([{"parameter": "PM2.5", "aqi": 126, "dataType": "O"}])) as p, \
         patch("utilities.air_quality.requests.get") as g:   # never hit the network
        assert air_quality.get_aqi() == 126
        g.assert_not_called()
        d = p.call_args.kwargs["data"]
        assert d["stateCode"] == "WA"
        assert d["latitude"] == 47.61 and d["longitude"] == -122.33


def test_aqi_zero_is_a_real_value_not_a_fallthrough():
    """Clean air reads 0. A `if not val:` check would discard it and fall back to
    the deliberately-demoted modelled source."""
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.post",
               return_value=_airnow([{"parameter": "PM2.5", "aqi": 0, "dataType": "O"}])), \
         patch("utilities.air_quality.requests.get") as g:
        assert air_quality.get_aqi() == 0
        g.assert_not_called()          # 0 is valid — must NOT fall through


# ── Open-Meteo fallback ─────────────────────────────────────────────────────

def test_falls_back_to_openmeteo_when_airnow_errors():
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.post", side_effect=Exception("boom")), \
         patch("utilities.air_quality.requests.get", return_value=_om(78)) as g:
        assert air_quality.get_aqi() == 78
        g.assert_called_once()


def test_falls_back_to_openmeteo_when_airnow_has_no_observed():
    """Forecast-only response (no dataType 'O') must fall through, not return 185."""
    _reset(); _set_cfg()
    entries = [{"parameter": "PM2.5", "aqi": 185, "dataType": "F"}]
    with patch("utilities.air_quality.requests.post", return_value=_airnow(entries)), \
         patch("utilities.air_quality.requests.get", return_value=_om(78)):
        assert air_quality.get_aqi() == 78


def test_no_state_skips_airnow_and_uses_openmeteo():
    _reset(state=""); _set_cfg()
    with patch("utilities.air_quality.requests.post") as p, \
         patch("utilities.air_quality.requests.get", return_value=_om(64)):
        assert air_quality.get_aqi() == 64
        p.assert_not_called()          # don't call AirNow without a state code


def test_both_sources_down_returns_none():
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.post", side_effect=Exception("x")), \
         patch("utilities.air_quality.requests.get", side_effect=Exception("y")):
        assert air_quality.get_aqi() is None


# ── gating ──────────────────────────────────────────────────────────────────

def test_disabled_makes_no_call():
    _reset(); _set_cfg(enabled=False)
    with patch("utilities.air_quality.requests.post") as p, \
         patch("utilities.air_quality.requests.get") as g:
        assert air_quality.get_aqi() is None
        p.assert_not_called(); g.assert_not_called()


def test_no_location_makes_no_call():
    _reset(); _set_cfg(loc=[0.0, 0.0])
    with patch("utilities.air_quality.requests.post") as p, \
         patch("utilities.air_quality.requests.get") as g:
        assert air_quality.get_aqi() is None
        p.assert_not_called(); g.assert_not_called()


def test_failure_keeps_last_good_and_gates_retry():
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.post",
               return_value=_airnow([{"parameter": "PM2.5", "aqi": 80, "dataType": "O"}])):
        assert air_quality.get_aqi() == 80
    air_quality._cached_ts = 1.0                     # force the interval to elapse
    with patch("utilities.air_quality.requests.post", side_effect=Exception("boom")) as p, \
         patch("utilities.air_quality.requests.get", side_effect=Exception("boom")):
        assert air_quality.get_aqi() == 80           # last good (still fresh) kept
        p.assert_called_once()
        assert air_quality._cached_ts > 1.0          # ts advanced (anti-hammer)


def test_stale_value_is_hidden():
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.post",
               return_value=_airnow([{"parameter": "PM2.5", "aqi": 80, "dataType": "O"}])):
        assert air_quality.get_aqi() == 80
    air_quality._value_ts = time.time() - air_quality._MAX_AGE - 100
    with patch("utilities.air_quality.requests.post", side_effect=Exception("boom")), \
         patch("utilities.air_quality.requests.get", side_effect=Exception("boom")):
        assert air_quality.get_aqi() is None         # stale -> hidden


def test_stale_disk_cache_not_resurrected_on_cold_start():
    air_quality._cached_aqi = None
    air_quality._cached_ts = 0.0
    air_quality._value_ts = 0.0
    air_quality._state = "CO"
    with open(air_quality._CACHE_FILE, "w") as f:
        json.dump({"aqi": 300, "ts": time.time() - air_quality._MAX_AGE - 100}, f)
    _set_cfg()
    with patch("utilities.air_quality.requests.post", side_effect=Exception("x")), \
         patch("utilities.air_quality.requests.get", side_effect=Exception("y")):
        assert air_quality.get_aqi() is None         # ancient value not resurrected


# ── home-state resolution ───────────────────────────────────────────────────

def test_state_is_reverse_geocoded_once_then_cached_on_disk():
    air_quality._state = None
    try:
        os.remove(air_quality._STATE_FILE)
    except OSError:
        pass
    r = MagicMock()
    r.json.return_value = {"address": {"ISO3166-2-lvl4": "US-CO"}}
    r.raise_for_status = lambda: None
    with patch("utilities.air_quality.requests.get", return_value=r) as g:
        assert air_quality._home_state([39.74, -104.99]) == "CO"
        assert g.call_count == 1
        air_quality._state = None                    # simulate a restart
        assert air_quality._home_state([39.74, -104.99]) == "CO"
        assert g.call_count == 1                     # served from disk, no re-query


def test_state_disk_cache_is_keyed_on_location():
    """Moving LOCATION_HOME must invalidate the cached state — otherwise the old
    stateCode is sent with new coords and AirNow returns the wrong region."""
    air_quality._state = ""; air_quality._state_loc = None; air_quality._state_try_ts = 0.0
    with open(air_quality._STATE_FILE, "w") as f:
        json.dump({"lat": 39.74, "lon": -104.99, "state": "CO"}, f)
    r = MagicMock()
    r.json.return_value = {"address": {"ISO3166-2-lvl4": "US-WA"}}
    r.raise_for_status = lambda: None
    with patch("utilities.air_quality.requests.get", return_value=r) as g:
        # different coords than the cache file -> must re-geocode, not reuse "CO"
        assert air_quality._home_state([47.61, -122.33]) == "WA"
        g.assert_called_once()


def test_failed_geocode_is_retryable_and_not_persisted():
    """A transient failure (no network at boot) must not pin the device to the
    fallback source forever, and must never be written to disk."""
    air_quality._state = ""; air_quality._state_loc = None; air_quality._state_try_ts = 0.0
    try:
        os.remove(air_quality._STATE_FILE)
    except OSError:
        pass
    with patch("utilities.air_quality.requests.get", side_effect=Exception("no network")):
        assert air_quality._home_state([39.74, -104.99]) == ""
    assert not os.path.exists(air_quality._STATE_FILE)   # failure NOT persisted

    air_quality._state_try_ts = 0.0                      # let the backoff elapse
    r = MagicMock()
    r.json.return_value = {"address": {"ISO3166-2-lvl4": "US-CO"}}
    r.raise_for_status = lambda: None
    with patch("utilities.air_quality.requests.get", return_value=r):
        assert air_quality._home_state([39.74, -104.99]) == "CO"   # recovers


def test_geocode_200_without_state_is_not_persisted():
    """A 200 body with no ISO state must stay retryable, not poison the disk cache."""
    air_quality._state = ""; air_quality._state_loc = None; air_quality._state_try_ts = 0.0
    try:
        os.remove(air_quality._STATE_FILE)
    except OSError:
        pass
    r = MagicMock()
    r.json.return_value = {"error": "Unable to geocode"}
    r.raise_for_status = lambda: None
    with patch("utilities.air_quality.requests.get", return_value=r):
        assert air_quality._home_state([39.74, -104.99]) == ""
    assert not os.path.exists(air_quality._STATE_FILE)


def test_failed_geocode_backs_off():
    """Don't re-query Nominatim on every poll after a failure."""
    air_quality._state = ""; air_quality._state_loc = None; air_quality._state_try_ts = 0.0
    with patch("utilities.air_quality.requests.get", side_effect=Exception("x")) as g:
        assert air_quality._home_state([39.74, -104.99]) == ""
        assert air_quality._home_state([39.74, -104.99]) == ""   # immediate retry
        g.assert_called_once()                                   # backed off


# ── EPA colour bands (the chip) ─────────────────────────────────────────────

def test_epa_colour_bands():
    import scenes.temperature as t
    from setup import colours
    assert t._aqi_colour(40) is colours.GREEN            # Good
    assert t._aqi_colour(75) is colours.YELLOW           # Moderate
    assert t._aqi_colour(125) is colours.LIGHT_ORANGE    # USG
    assert t._aqi_colour(175) is colours.RED             # Unhealthy
    assert t._aqi_colour(250) is colours.PURPLE          # Very Unhealthy
    assert t._aqi_colour(400) is t.AQI_MAROON            # Hazardous
    # guard against the vacuous-mock regression: the constants must be distinct
    assert colours.GREEN is not t.AQI_MAROON
