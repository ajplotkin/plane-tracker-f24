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
    for f in (air_quality._CACHE_FILE, air_quality._STATE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass


def _set_cfg(enabled=True, loc=None):
    cfg.AQI_ALERTS_ENABLED = enabled
    cfg.LOCATION_HOME = loc if loc is not None else [39.74, -104.99]   # Denver — synthetic


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
        {"reportingArea": "Long Island Region", "parameter": "PM2.5", "aqi": 93, "dataType": "O"},
        {"reportingArea": "Long Island Region", "parameter": "OZONE", "aqi": 19, "dataType": "O"},
        {"reportingArea": "Long Island Region", "parameter": "PM2.5", "aqi": 185, "dataType": "F"},
        {"reportingArea": "Long Island Region", "parameter": "OZONE", "aqi": 77, "dataType": "F"},
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
               return_value=_airnow([{"parameter": "PM2.5", "aqi": 126, "dataType": "O"}])) as p:
        assert air_quality.get_aqi() == 126
        d = p.call_args.kwargs["data"]
        assert d["stateCode"] == "WA"
        assert d["latitude"] == 47.61 and d["longitude"] == -122.33


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
