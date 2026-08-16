"""rain.py (OWM minutely precip + wind) and tides.py (NOAA predictions + water
temp) — the non-blocking fetch contract and the parsing behind it.

Both modules used to do a synchronous requests.get on the render thread (rain
from the 1 Hz clock scene, tides from the 1 Hz date scene), so a WAN hang froze
the LED scroll for the full timeout. They now dispatch a background worker and
return the last good value immediately — same pattern as airport_status.py /
nws_alerts.py / iss.py.

Every test mocks requests (no network) and redirects the on-disk caches to a
scratch dir, so nothing here can write phantom data into the real .cache/ that
the display and web processes read. settle() joins the worker INSIDE the patch
block so it can never outlive the mock and hit the real endpoints.
"""
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import config as cfg
from tests.conftest import settle
from utilities import rain, tides

# Redirect both modules' disk caches into scratch space.
_TMP = tempfile.mkdtemp(prefix="rain_tides_test_")
rain._CACHE_DIR = _TMP
rain._CACHE_FILE = os.path.join(_TMP, "rain.json")
tides._CACHE_DIR = _TMP
tides._CACHE_FILE = os.path.join(_TMP, "tides.json")


def _rm_caches():
    for f in (rain._CACHE_FILE, tides._CACHE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass


def _json_resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status = lambda: None
    return r


def _text_resp(text):
    r = MagicMock()
    r.text = text
    r.raise_for_status = lambda: None
    return r


# ══ rain.py ═════════════════════════════════════════════════════════════════

def _reset_rain():
    rain._cached_data = None
    rain._cached_ts = 0.0
    rain._refresh_pending = False
    cfg.OWM_API_KEY = "testkey"
    cfg.LOCATION_HOME = [40.96, -72.18]
    _rm_caches()


def _owm(precip, weather_id=500, wind_ms=0.0):
    """OWM One Call payload with a minutely precipitation series (mm/h)."""
    return {
        "current": {"weather": [{"id": weather_id}], "wind_speed": wind_ms},
        "minutely": [{"precipitation": p} for p in precip],
    }


def _poll_rain(**patch_kwargs):
    """One full rain refresh: dispatch + join the worker. Returns the mock."""
    with patch("utilities.rain.requests.get", **patch_kwargs) as g:
        rain.get_rain_alert()
        settle(rain)
        return g


def test_rain_fetch_caches_and_parses_start_and_stop():
    _reset_rain()
    # dry now, precip from minute 8 on
    g = _poll_rain(return_value=_json_resp(_owm([0.0] * 8 + [0.5] * 5)))
    g.assert_called_once()
    assert rain.get_rain_alert() == {"type": "rain", "action": "starting", "minutes": 8}
    assert g.call_count == 1          # gated: served from the in-memory cache

    # raining now, stops at minute 3 — snow code (601) drives the type
    _reset_rain()
    _poll_rain(return_value=_json_resp(_owm([0.4, 0.4, 0.4, 0.0], weather_id=601)))
    assert rain.get_rain_alert() == {"type": "snow", "action": "stopping", "minutes": 3}

    # raining with no let-up in the window -> "now", no minutes key
    _reset_rain()
    _poll_rain(return_value=_json_resp(_owm([0.4] * 10)))
    assert rain.get_rain_alert() == {"type": "rain", "action": "now"}

    # dry for the whole window -> no alert at all
    _reset_rain()
    _poll_rain(return_value=_json_resp(_owm([0.0] * 10)))
    assert rain.get_rain_alert() is None


def test_wind_info_uses_the_same_cached_payload():
    _reset_rain()
    _poll_rain(return_value=_json_resp(_owm([0.0] * 5, wind_ms=11.2)))   # ~25 mph
    with patch("utilities.rain.requests.get") as g:
        assert rain.get_wind_info() == {"text": "Wind 25", "color": "cyan"}
        g.assert_not_called()          # no second fetch for the wind
    # below the threshold -> nothing shown
    _reset_rain()
    _poll_rain(return_value=_json_resp(_owm([0.0] * 5, wind_ms=4.0)))    # ~8 mph
    assert rain.get_wind_info() is None


def test_rain_getter_never_blocks_on_a_hanging_owm():
    """A WAN hang used to freeze the 1 Hz clock scene for the full read timeout."""
    _reset_rain()
    _poll_rain(return_value=_json_resp(_owm([0.0] * 8 + [0.5])))
    assert rain.get_rain_alert()["minutes"] == 8

    release = threading.Event()

    def hang(*a, **kw):
        release.wait(10)
        return _json_resp(_owm([0.5] * 10))

    rain._cached_ts = 0.0              # force the poll interval to have elapsed
    with patch("utilities.rain.requests.get", side_effect=hang):
        t0 = time.monotonic()
        assert rain.get_rain_alert()["minutes"] == 8    # last good, immediately
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"getter blocked for {elapsed:.2f}s"
        release.set()
        settle(rain)
        assert rain.get_rain_alert() == {"type": "rain", "action": "now"}


def test_rain_one_refresh_in_flight_across_both_getters():
    """get_rain_alert() and get_wind_info() both call _refresh(); together they
    must still produce exactly one worker and one HTTP call."""
    _reset_rain()
    release = threading.Event()
    calls = []

    def hang(*a, **kw):
        calls.append(1)
        release.wait(10)
        return _json_resp(_owm([0.0] * 5))

    with patch("utilities.rain.requests.get", side_effect=hang):
        rain.get_rain_alert()          # dispatches the one worker
        for _ in range(3):
            rain._cached_ts = 0.0      # gate wide open again
            assert rain.get_rain_alert() is None
            assert rain.get_wind_info() is None
            assert rain._cached_ts == 0.0   # gate NOT consumed while in flight
        release.set()
        settle(rain)
    assert calls == [1]


def test_rain_failure_keeps_last_good_and_gates_the_retry():
    _reset_rain()
    _poll_rain(return_value=_json_resp(_owm([0.0] * 8 + [0.5])))
    assert rain.get_rain_alert()["minutes"] == 8
    rain._cached_ts = 0.0
    with patch("utilities.rain.requests.get", side_effect=Exception("boom")) as g:
        assert rain.get_rain_alert()["minutes"] == 8   # last good returned at once
        settle(rain)
        g.assert_called_once()
        assert rain._cached_ts > 0                     # ts advanced (anti-hammer)
        assert rain.get_rain_alert()["minutes"] == 8   # failure kept the last good
        g.assert_called_once()                         # and did not re-hammer


def test_rain_cold_start_failure_does_not_hammer_every_frame():
    """The original bug: with NO last-good value, a failing fetch advanced nothing,
    so the 1 Hz scene re-dispatched on EVERY frame. Cold start + persistent failure
    must still cost exactly one HTTP call until the retry gate expires."""
    _reset_rain()
    assert rain._cached_data is None          # cold: nothing ever succeeded
    with patch("utilities.rain.requests.get", side_effect=Exception("boom")) as g:
        assert rain.get_rain_alert() is None
        settle(rain)
        g.assert_called_once()
        # simulate the next 10 render frames
        for _ in range(10):
            assert rain.get_rain_alert() is None
        settle(rain)
        g.assert_called_once()                # gate held: still exactly one call


def test_rain_unconfigured_makes_no_call():
    _reset_rain()
    cfg.OWM_API_KEY = ""
    with patch("utilities.rain.requests.get") as g:
        assert rain.get_rain_alert() is None
        g.assert_not_called()
    _reset_rain()
    cfg.LOCATION_HOME = [0.0, 0.0]
    with patch("utilities.rain.requests.get") as g:
        assert rain.get_rain_alert() is None
        g.assert_not_called()
    _reset_rain()


# ══ tides.py ════════════════════════════════════════════════════════════════

def _reset_tides(station="9999999", water="9999999", fallback="", fb_enabled=True):
    tides._cached_tides = None
    tides._cached_date = None
    tides._tide_next_retry = 0.0
    tides._water_temp = None
    tides._water_temp_ts = 0.0
    tides._water_temp_is_fallback = False
    tides._refresh_pending = False
    tides.TIDE_STATION = station
    tides.WATER_TEMP_STATION = water
    tides.WATER_TEMP_FALLBACK_STATION = fallback
    tides.WATER_TEMP_FALLBACK_ENABLED = fb_enabled
    tides.CLOCK_FORMAT = "12hr"
    tides.TEMPERATURE_UNITS = "imperial"
    _rm_caches()


def _pred(dt, kind):
    return {"t": dt.strftime("%Y-%m-%d %H:%M"), "type": kind}


def _expect_12hr(dt):
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'a' if dt.hour < 12 else 'p'}"


def test_tide_predictions_dispatch_then_format_next_high_and_low():
    _reset_tides()
    high = datetime.now() + timedelta(hours=1)
    low = datetime.now() + timedelta(hours=2)
    payload = {"predictions": [_pred(high, "H"), _pred(low, "L")]}
    with patch("utilities.tides.requests.get", return_value=_json_resp(payload)) as g:
        assert tides.get_next_tides() is None      # nothing to show until it lands
        settle(tides)
        g.assert_called_once()
        assert tides.get_next_tides() == {"high": _expect_12hr(high),
                                          "low": _expect_12hr(low)}
        g.assert_called_once()                     # once per day, not per frame
        assert g.call_args.kwargs["params"]["station"] == "9999999"


def test_tide_getter_never_blocks_and_retry_is_gated_to_5min():
    """A NOAA outage used to drive a blocking fetch on the render thread; now the
    dispatch is gated to one attempt per 5 minutes and never waits."""
    _reset_tides()
    release = threading.Event()

    def hang(*a, **kw):
        release.wait(10)
        return _json_resp({"predictions": []})     # empty -> _fetch_predictions None

    with patch("utilities.tides.requests.get", side_effect=hang) as g:
        t0 = time.monotonic()
        assert tides.get_next_tides() is None
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"getter blocked for {elapsed:.2f}s"
        release.set()
        settle(tides)
        assert g.call_count == 1
        # the failure advanced the retry gate — no re-dispatch on the next frames
        assert tides._tide_next_retry > time.time() + 250
        for _ in range(3):
            assert tides.get_next_tides() is None
        assert g.call_count == 1

    # once the gate elapses it retries, and a good response lands
    high = datetime.now() + timedelta(hours=3)
    tides._tide_next_retry = 0.0
    with patch("utilities.tides.requests.get",
               return_value=_json_resp({"predictions": [_pred(high, "H")]})):
        tides.get_next_tides()
        settle(tides)
        assert tides.get_next_tides()["high"] == _expect_12hr(high)


def test_water_temp_primary_then_fallback_and_last_good():
    # primary CO-OPS station answers
    _reset_tides()
    with patch("utilities.tides.requests.get",
               return_value=_json_resp({"data": [{"v": "71.6"}]})) as g:
        tides.get_water_temp()
        settle(tides)
        assert tides.get_water_temp() == "72"
        assert tides.is_water_temp_fallback() is False
        g.assert_called_once()

    # primary empty -> NDBC buoy fallback (5-digit numeric, Celsius -> F)
    _reset_tides(fallback="44017")
    def primary_empty(url, **kw):
        if url.endswith("44017.txt"):
            return _text_resp("#YY MM DD hh mm WDIR WSPD WTMP\n#yr mo dy hr mn degT m/s degC\n"
                              "2026 08 13 12 00 180 5.0 21.0\n")
        return _json_resp({"data": []})
    with patch("utilities.tides.requests.get", side_effect=primary_empty):
        tides.get_water_temp()
        settle(tides)
        assert tides.get_water_temp() == "70"          # 21.0C -> 69.8F -> "70"
        assert tides.is_water_temp_fallback() is True

    # both down -> keep the last good value, gate the retry
    tides._water_temp_ts = 0.0
    with patch("utilities.tides.requests.get", side_effect=Exception("boom")) as g:
        assert tides.get_water_temp() == "70"          # last good, immediately
        settle(tides)
        assert g.call_count == 2                       # primary + fallback tried
        assert tides._water_temp_ts > 0                # ts advanced (anti-hammer)
        assert tides.get_water_temp() == "70"
        assert g.call_count == 2                       # and did not re-hammer


def test_water_temp_fallback_can_be_disabled():
    _reset_tides(fallback="44017", fb_enabled=False)
    with patch("utilities.tides.requests.get",
               return_value=_json_resp({"data": []})) as g:
        tides.get_water_temp()
        settle(tides)
        assert tides.get_water_temp() is None
        g.assert_called_once()                         # primary only


def test_water_temp_getter_never_blocks():
    _reset_tides()
    with patch("utilities.tides.requests.get",
               return_value=_json_resp({"data": [{"v": "68.2"}]})):
        tides.get_water_temp()
        settle(tides)
    assert tides.get_water_temp() == "68"

    release = threading.Event()

    def hang(*a, **kw):
        release.wait(10)
        return _json_resp({"data": [{"v": "75.0"}]})

    tides._water_temp_ts = 0.0
    with patch("utilities.tides.requests.get", side_effect=hang):
        t0 = time.monotonic()
        assert tides.get_water_temp() == "68"          # last good, immediately
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"getter blocked for {elapsed:.2f}s"
        release.set()
        settle(tides)
        assert tides.get_water_temp() == "75"


def test_tides_one_refresh_in_flight_across_both_getters():
    """Predictions and the water temp share one worker slot; a blocked dispatch
    must not consume either gate."""
    _reset_tides()
    release = threading.Event()
    calls = []

    def hang(*a, **kw):
        calls.append(1)
        release.wait(10)
        return _json_resp({"data": [{"v": "70.0"}]})

    with patch("utilities.tides.requests.get", side_effect=hang):
        tides.get_water_temp()                 # dispatches the one worker
        gate = tides._tide_next_retry
        for _ in range(3):
            tides._water_temp_ts = 0.0
            assert tides.get_next_tides() is None
            assert tides.get_water_temp() is None
            assert tides._water_temp_ts == 0.0        # water-temp gate untouched
            assert tides._tide_next_retry == gate     # prediction gate untouched
        release.set()
        settle(tides)
    assert calls == [1]


def test_tides_unconfigured_makes_no_call():
    _reset_tides(station="", water="")
    with patch("utilities.tides.requests.get") as g:
        assert tides.get_next_tides() is None
        assert tides.get_water_temp() is None
        g.assert_not_called()
