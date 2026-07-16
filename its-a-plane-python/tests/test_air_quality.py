"""Open-Meteo AQI fetcher + the EPA colour bands for the chip.

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

# Redirect the on-disk cache to a scratch dir so the success-path tests can't
# write phantom AQI data into the real its-a-plane-python/.cache/air_quality.json
# (which the display + web processes read).
_TMP = tempfile.mkdtemp(prefix="aqi_test_")
air_quality._CACHE_DIR = _TMP
air_quality._CACHE_FILE = os.path.join(_TMP, "air_quality.json")


def _reset():
    air_quality._cached_aqi = None
    air_quality._cached_ts = 1.0    # non-zero -> skip the cold-start disk seed
    air_quality._value_ts = 0.0
    try:
        os.remove(air_quality._CACHE_FILE)
    except OSError:
        pass


def _set_cfg(enabled=True, loc=None):
    cfg.AQI_ALERTS_ENABLED = enabled
    cfg.LOCATION_HOME = loc if loc is not None else [40.9, -72.3]


def _resp(us_aqi):
    """An Open-Meteo air-quality response: {"current": {"us_aqi": N}}."""
    r = MagicMock()
    r.json.return_value = {"current": {"us_aqi": us_aqi}}
    r.raise_for_status = lambda: None
    return r


def _write_cache(aqi, age_s):
    with open(air_quality._CACHE_FILE, "w") as f:
        json.dump({"aqi": aqi, "ts": time.time() - age_s}, f)


# ── fetcher ─────────────────────────────────────────────────────────────────

def test_returns_us_aqi_and_requests_home_coords():
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.get", return_value=_resp(78)) as g:
        assert air_quality.get_aqi() == 78
        p = g.call_args.kwargs["params"]
        assert p["latitude"] == 40.9 and p["longitude"] == -72.3
        assert p["current"] == "us_aqi"      # overall index (incl. PM2.5)
        assert "API_KEY" not in p            # keyless


def test_float_aqi_is_rounded_to_int():
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.get", return_value=_resp(66.6)):
        assert air_quality.get_aqi() == 67


def test_disabled_makes_no_http_call():
    _reset(); _set_cfg(enabled=False)
    with patch("utilities.air_quality.requests.get") as g:
        assert air_quality.get_aqi() is None
        g.assert_not_called()


def test_no_location_makes_no_http_call():
    _reset(); _set_cfg(loc=[0.0, 0.0])
    with patch("utilities.air_quality.requests.get") as g:
        assert air_quality.get_aqi() is None
        g.assert_not_called()


def test_missing_or_bad_value_keeps_none():
    _reset(); _set_cfg()
    r = MagicMock()
    r.json.return_value = {"current": {}}      # no us_aqi key
    r.raise_for_status = lambda: None
    with patch("utilities.air_quality.requests.get", return_value=r):
        assert air_quality.get_aqi() is None


def test_failure_keeps_last_good_and_gates_retry():
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.get", return_value=_resp(80)):
        assert air_quality.get_aqi() == 80
    air_quality._cached_ts = 1.0                     # force the interval to elapse
    with patch("utilities.air_quality.requests.get",
               side_effect=Exception("boom")) as g:
        assert air_quality.get_aqi() == 80           # last good (still fresh) kept
        g.assert_called_once()
        assert air_quality._cached_ts > 1.0          # ts advanced (anti-hammer)


def test_stale_value_is_hidden():
    """A value not refreshed within _MAX_AGE ages out to None (not shown wrongly
    forever while fetches keep failing)."""
    _reset(); _set_cfg()
    with patch("utilities.air_quality.requests.get", return_value=_resp(80)):
        assert air_quality.get_aqi() == 80
    # age the last-good value past the freshness bound without a successful refresh
    air_quality._value_ts = time.time() - air_quality._MAX_AGE - 100
    with patch("utilities.air_quality.requests.get",
               side_effect=Exception("boom")):
        assert air_quality.get_aqi() is None         # stale -> hidden


def test_fresh_disk_cache_is_seeded_on_cold_start():
    air_quality._cached_aqi = None
    air_quality._cached_ts = 0.0                      # cold start -> allow the seed
    air_quality._value_ts = 0.0
    _write_cache(90, age_s=60)                        # 1 min old = fresh
    _set_cfg()
    with patch("utilities.air_quality.requests.get",
               side_effect=Exception("boom")):
        assert air_quality.get_aqi() == 90            # fresh disk value shown


def test_stale_disk_cache_not_resurrected_on_cold_start():
    """A restart during an outage must NOT pin an ancient AQI."""
    air_quality._cached_aqi = None
    air_quality._cached_ts = 0.0
    air_quality._value_ts = 0.0
    _write_cache(300, age_s=air_quality._MAX_AGE + 100)   # ancient
    _set_cfg()
    with patch("utilities.air_quality.requests.get",
               side_effect=Exception("boom")) as g:
        assert air_quality.get_aqi() is None          # ancient value not resurrected
        g.assert_called_once()                        # and a real refresh was attempted


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
