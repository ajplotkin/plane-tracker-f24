"""utilities/temperature.py — the non-blocking fetch contract, and the rate
limiting / 429 backoff / caching semantics it had to preserve.

This was the last fetcher still doing synchronous HTTP on the render thread: a
`requests.Session` GET (realtime) and POST (forecast), both `timeout=(5, 20)`
behind a urllib3 Retry, called from the 1 Hz temperature/clock/date/forecast
scenes. At 10 FPS any frame over 100 ms is a visible scroll freeze, so a slow
Tomorrow.io round-trip froze the panel for up to a minute. Both fetches now run
in one shared background worker and every public getter returns the last good
value immediately.

Every test mocks the session (no network) and redirects the on-disk caches to a
scratch dir, so nothing here can write phantom data into the real .cache/ that
the display and web mirror processes read. settle() joins the worker INSIDE the
patch block so it can never outlive the mock and hit the real API.
"""
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import settle
from utilities import temperature as t

# Redirect every on-disk cache this module writes into scratch space.
_TMP = tempfile.mkdtemp(prefix="temperature_test_")
t._CACHE_DIR = _TMP
t._TEMP_CACHE_FILE = os.path.join(_TMP, "temperature.json")
t._FORECAST_CACHE_FILE = os.path.join(_TMP, "forecast.json")
t._HOURLY_UV_CACHE_FILE = os.path.join(_TMP, "hourly_uv.json")

_ICON = "10000"          # a real file in icons/ (247 of them; we warm ~3)


def _reset(api_key="testkey"):
    """Cold module: no caches, no rate limit, no backoff, nothing in flight."""
    t.TOMORROW_API_KEY = api_key
    t.TEMPERATURE_LOCATION = "40.96,-72.18"
    t.TEMPERATURE_UNITS = "imperial"
    t.FORECAST_DAYS = 3
    t._cached_temp = None
    t._cached_temp_ts = 0.0
    t._cached_forecast = None
    t._cached_forecast_ts = 0.0
    t._cached_hourly_uv = []
    t._hourly_file_mtime = 0.0
    t._hourly_file_cache = []
    t._temp_last_call_ts = 0.0
    t._fc_last_call_ts = 0.0
    t._in_backoff = False
    t._backoff_entered_ts = 0.0
    t._temp_dispatch_ts = 0.0
    t._fc_dispatch_ts = 0.0
    t._refresh_pending = False
    for f in (t._TEMP_CACHE_FILE, t._FORECAST_CACHE_FILE, t._HOURLY_UV_CACHE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass


def _open_the_gates():
    """Make both fetches eligible again without touching the cached values."""
    t._cached_temp_ts = 0.0
    t._cached_forecast_ts = 0.0
    t._temp_last_call_ts = 0.0
    t._fc_last_call_ts = 0.0
    t._temp_dispatch_ts = 0.0
    t._fc_dispatch_ts = 0.0


def _realtime(temp=71.6, humidity=52.0, uv=4, status=200):
    r = MagicMock()
    r.status_code = status
    r.raise_for_status = lambda: None
    r.json.return_value = {"data": {"values": {
        "temperature": temp, "humidity": humidity, "uvIndex": uv}}}
    return r


def _daily(days=3, icon=_ICON, tmin=60.0, tmax=80.0):
    base = datetime.now().astimezone().replace(microsecond=0)
    return [{
        "startTime": (base + timedelta(days=i)).isoformat(),
        "values": {
            "temperatureMin": tmin, "temperatureMax": tmax,
            "weatherCodeFullDay": icon,
            "sunriseTime": (base + timedelta(days=i)).isoformat(),
            "sunsetTime": (base + timedelta(days=i, hours=12)).isoformat(),
            "moonPhase": 3, "uvIndex": 5,
        },
    } for i in range(days)]


def _hourly(uv_points=(2.0, 8.0)):
    """An hourly timeline straddling "now" so get_current_uv() interpolates."""
    now = datetime.now().astimezone().replace(microsecond=0)
    starts = [now - timedelta(minutes=30), now + timedelta(minutes=30)]
    return [{"startTime": s.isoformat(), "values": {"uvIndex": u}}
            for s, u in zip(starts, uv_points)]


def _forecast_resp(days=3, icon=_ICON, uv_points=(2.0, 8.0), status=200):
    r = MagicMock()
    r.status_code = status
    r.raise_for_status = lambda: None
    r.json.return_value = {"data": {"timelines": [
        {"timestep": "1h", "intervals": _hourly(uv_points)},
        {"timestep": "1d", "intervals": _daily(days, icon)},
    ]}}
    return r


def _session(get=None, post=None):
    """A stand-in for the module's shared requests.Session."""
    s = MagicMock()
    if get is not None:
        s.get = get
    if post is not None:
        s.post = post
    return s


def _patch_session(get=None, post=None):
    return patch("utilities.temperature.get_session",
                 return_value=_session(get, post))


# ══ non-blocking contract ═══════════════════════════════════════════════════
# The budget assertions below are what make these tests fail against a
# synchronous implementation: the mock blocks for up to 10 s, so a getter that
# waits for it cannot come back inside 0.5 s.

def test_temperature_getter_never_blocks_on_a_hanging_api():
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime())):
        t.grab_temperature_and_humidity()
        settle(t)
    assert t.grab_temperature_and_humidity() == (71.6, 52.0)

    release = threading.Event()

    def hang(*a, **kw):
        release.wait(10)
        return _realtime(temp=80.0, humidity=40.0, uv=9)

    _open_the_gates()
    with _patch_session(get=MagicMock(side_effect=hang)):
        t0 = time.monotonic()
        assert t.grab_temperature_and_humidity() == (71.6, 52.0)   # last good, now
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"getter blocked for {elapsed:.2f}s"
        release.set()
        settle(t)
        assert t.grab_temperature_and_humidity() == (80.0, 40.0)
        assert t.get_uv_index() == 9


def test_forecast_getter_never_blocks_on_a_hanging_api():
    _reset()
    release = threading.Event()

    def hang(*a, **kw):
        release.wait(10)
        return _forecast_resp()

    with _patch_session(post=MagicMock(side_effect=hang)):
        t0 = time.monotonic()
        assert t.grab_forecast(tag="test") == []       # nothing yet, immediately
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"getter blocked for {elapsed:.2f}s"
        release.set()
        settle(t)
        forecast = t.grab_forecast(tag="test")
        assert len(forecast) == 3
        assert forecast[0]["values"]["weatherCodeFullDay"] == _ICON


def test_render_thread_budget_over_many_frames():
    """Ten seconds of 1 Hz render frames against a dead API must cost the render
    thread effectively nothing — this is the whole point of the change."""
    _reset()
    release = threading.Event()

    def hang(*a, **kw):
        release.wait(10)
        raise t.RequestException("timeout")

    with _patch_session(get=MagicMock(side_effect=hang),
                        post=MagicMock(side_effect=hang)):
        t0 = time.monotonic()
        for _ in range(10):
            t.grab_temperature_and_humidity()
            t.grab_forecast(tag="test")
            t.get_uv_index()
            t.get_current_uv()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"10 frames of getters took {elapsed:.2f}s"
        release.set()
        settle(t)


# ══ anti-hammer gate ════════════════════════════════════════════════════════

def test_cold_start_failure_does_not_hammer_every_frame():
    """With NO last-good value the fetch fails at the network level, so
    _record_call() never fires and the rate limiter stays open. Without the
    dispatch gate the 1 Hz scenes would spawn a worker on every frame."""
    _reset()
    get = MagicMock(side_effect=t.RequestException("boom"))
    with _patch_session(get=get):
        assert t.grab_temperature_and_humidity() == (None, None)
        settle(t)
        get.assert_called_once()
        for _ in range(10):                    # the next ten render frames
            assert t.grab_temperature_and_humidity() == (None, None)
        settle(t)
        get.assert_called_once()               # gate held: still exactly one
    assert t._temp_dispatch_ts > 0             # advanced despite the failure


def test_forecast_cold_start_failure_is_gated_too():
    _reset()
    post = MagicMock(side_effect=t.RequestException("boom"))
    with _patch_session(post=post):
        assert t.grab_forecast(tag="days") == []
        settle(t)
        for _ in range(10):
            assert t.grab_forecast(tag="days") == []
        settle(t)
        post.assert_called_once()
    assert t._fc_dispatch_ts > 0


def test_in_flight_refresh_does_not_consume_either_gate():
    """The realtime and forecast fetches share ONE worker slot. A getter that
    finds it busy must return last-good WITHOUT burning its dispatch gate, so
    the fetch it wanted still happens on a later frame."""
    _reset()
    release = threading.Event()
    calls = []

    def hang(*a, **kw):
        calls.append(1)
        release.wait(10)
        return _realtime()

    threads = []
    real_thread = t.threading.Thread

    def count(*a, **kw):
        th = real_thread(*a, **kw)
        threads.append(th)
        return th

    with patch("utilities.temperature.threading.Thread", side_effect=count), \
            _patch_session(get=MagicMock(side_effect=hang),
                           post=MagicMock(side_effect=hang)):
        t.grab_temperature_and_humidity()      # dispatches the one worker
        for _ in range(3):
            t._temp_dispatch_ts = 0.0          # gates wide open again
            t._fc_dispatch_ts = 0.0
            assert t._refresh_pending is True
            assert t.grab_temperature_and_humidity() == (None, None)
            assert t.grab_forecast(tag="test") == []
            assert t._temp_dispatch_ts == 0.0  # realtime gate untouched
            assert t._fc_dispatch_ts == 0.0    # forecast gate untouched
        release.set()
        settle(t)
    assert calls == [1]                        # exactly one HTTP call...
    assert len(threads) == 1                   # ...from exactly one worker


def test_dispatch_gate_is_restored_when_thread_start_fails():
    """Thread.start() raising (OOM on the 512MB Pi) must leave the module able
    to retry on the next frame, not wedged with a consumed gate."""
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime())):
        with patch("utilities.temperature.threading.Thread",
                   side_effect=RuntimeError("can't start new thread")):
            assert t.grab_temperature_and_humidity() == (None, None)
        assert t._temp_dispatch_ts == 0.0      # gate restored
        assert t._refresh_pending is False     # not wedged
        # the very next frame retries and succeeds
        t.grab_temperature_and_humidity()
        settle(t)
    assert t.grab_temperature_and_humidity() == (71.6, 52.0)


# ══ last-good on error ══════════════════════════════════════════════════════

def test_failure_keeps_the_last_good_value_and_the_disk_cache():
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime()),
                        post=MagicMock(return_value=_forecast_resp())):
        t.grab_temperature_and_humidity()
        settle(t)
        t.grab_forecast(tag="test")
        settle(t)
    assert t.grab_temperature_and_humidity() == (71.6, 52.0)
    good_forecast = t.grab_forecast(tag="test")
    assert len(good_forecast) == 3

    _open_the_gates()
    with _patch_session(get=MagicMock(side_effect=t.RequestException("dns")),
                        post=MagicMock(side_effect=t.RequestException("dns"))):
        assert t.grab_temperature_and_humidity() == (71.6, 52.0)
        settle(t)
        assert t.grab_temperature_and_humidity() == (71.6, 52.0)
        _open_the_gates()
        assert t.grab_forecast(tag="test") == good_forecast
        settle(t)
        assert t.grab_forecast(tag="test") == good_forecast
    # the on-disk copies the web mirror reads are untouched by the failure
    with open(t._TEMP_CACHE_FILE) as f:
        assert json.load(f)["data"] == [71.6, 52.0, 4]


def test_incomplete_payload_keeps_the_last_good_value():
    """A 200 with missing fields must not blank the display."""
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime())):
        t.grab_temperature_and_humidity()
        settle(t)
    _open_the_gates()
    partial = MagicMock(status_code=200, raise_for_status=lambda: None)
    partial.json.return_value = {"data": {"values": {"temperature": 90.0}}}
    with _patch_session(get=MagicMock(return_value=partial)):
        t.grab_temperature_and_humidity()
        settle(t)
    assert t.grab_temperature_and_humidity() == (71.6, 52.0)


# ══ rate limiting (preserved) ═══════════════════════════════════════════════

def test_free_tier_rate_limit_still_blocks_the_dispatch():
    """The 30-min per-endpoint limit is what keeps us inside Tomorrow.io's free
    tier. A stale cache with a wide-open dispatch gate must STILL make no call
    while the rate limiter says no."""
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime())):
        t.grab_temperature_and_humidity()
        settle(t)
    assert t._temp_last_call_ts > 0             # the worker recorded the call

    t._cached_temp_ts = 0.0                     # cache stale
    t._temp_dispatch_ts = 0.0                   # dispatch gate wide open
    get = MagicMock(return_value=_realtime(temp=99.0))
    with _patch_session(get=get):
        for _ in range(5):
            assert t.grab_temperature_and_humidity() == (71.6, 52.0)
        settle(t)
        get.assert_not_called()                 # rate limiter held

    # ...and once the interval has elapsed it does fetch again
    t._temp_last_call_ts = time.time() - (t._NORMAL_INTERVAL_S + 1)
    with _patch_session(get=get):
        t.grab_temperature_and_humidity()
        settle(t)
        get.assert_called_once()
    assert t.grab_temperature_and_humidity()[0] == 99.0


def test_forecast_rate_limit_still_blocks_the_dispatch():
    """Same guarantee on the forecast endpoint — its 900 s TTL expires twice per
    rate-limit window, so this is the gate that actually does the work."""
    _reset()
    with _patch_session(post=MagicMock(return_value=_forecast_resp(days=3))):
        t.grab_forecast(tag="test")
        settle(t)
    assert t._fc_last_call_ts > 0
    assert len(t.grab_forecast(tag="test")) == 3

    t._cached_forecast_ts = 0.0                 # cache stale
    t._fc_dispatch_ts = 0.0                     # dispatch gate wide open
    post = MagicMock(return_value=_forecast_resp(days=5))
    with _patch_session(post=post):
        for _ in range(5):
            assert len(t.grab_forecast(tag="test")) == 3
        settle(t)
        post.assert_not_called()                # rate limiter held

    t._fc_last_call_ts = time.time() - (t._NORMAL_INTERVAL_S + 1)
    with _patch_session(post=post):
        t.grab_forecast(tag="test")
        settle(t)
        post.assert_called_once()
    assert len(t.grab_forecast(tag="test")) == 5


def test_a_fresh_cache_is_served_without_touching_any_gate():
    """The TTLs come first: a fresh value must be returned with no rate-limiter
    or dispatch work at all, however wide open those gates are."""
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime()),
                        post=MagicMock(return_value=_forecast_resp())):
        t.grab_temperature_and_humidity()
        settle(t)
        t.grab_forecast(tag="test")
        settle(t)

    t._temp_last_call_ts = t._fc_last_call_ts = 0.0     # rate limiter open
    t._temp_dispatch_ts = t._fc_dispatch_ts = 0.0       # dispatch gates open
    get, post = MagicMock(), MagicMock()
    with _patch_session(get=get, post=post):
        for _ in range(5):
            assert t.grab_temperature_and_humidity() == (71.6, 52.0)
            assert len(t.grab_forecast(tag="test")) == 3
        settle(t)
        get.assert_not_called()
        post.assert_not_called()
    assert t._temp_dispatch_ts == 0.0            # gates never even consulted
    assert t._fc_dispatch_ts == 0.0
    assert t._TEMP_CACHE_TTL == 3600
    assert t._FORECAST_CACHE_TTL == 900


def test_the_two_endpoints_rate_limit_independently():
    """Sharing one worker slot must not merge the two rate limiters — a realtime
    call may not lock out the forecast (their TTLs are 3600 s vs 900 s)."""
    _reset()
    t._record_call("temp")
    assert t._rate_limited("temp") is True
    assert t._rate_limited("forecast") is False
    with _patch_session(post=MagicMock(return_value=_forecast_resp())) as _:
        assert len(t.grab_forecast(tag="test")) == 0   # nothing cached yet
        settle(t)
    assert len(t.grab_forecast(tag="test")) == 3       # forecast got through


def test_normal_interval_is_still_thirty_minutes():
    assert t._NORMAL_INTERVAL_S == 1800
    assert t._BACKOFF_INTERVAL_S == 600
    assert t._BACKOFF_AUTO_CLEAR_S == 7200


# ══ 429 backoff (preserved) ═════════════════════════════════════════════════

def test_429_enters_backoff_records_the_call_and_keeps_the_cache():
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime())):
        t.grab_temperature_and_humidity()
        settle(t)
    assert t.grab_temperature_and_humidity() == (71.6, 52.0)

    _open_the_gates()
    limited = MagicMock(status_code=429)
    limited.raise_for_status = MagicMock(
        side_effect=AssertionError("429 must be handled before raise_for_status"))
    limited.json.side_effect = AssertionError("429 body must not be parsed")
    with _patch_session(get=MagicMock(return_value=limited)):
        assert t.grab_temperature_and_humidity() == (71.6, 52.0)
        settle(t)
    assert t._in_backoff is True
    assert t._temp_last_call_ts > 0                       # the 429 counted
    assert t.grab_temperature_and_humidity() == (71.6, 52.0)   # cache intact


def test_backoff_shortens_the_interval_then_a_success_clears_it():
    _reset()
    # 700 s since the last call: rate-limited at the normal 1800 s interval...
    t._temp_last_call_ts = time.time() - 700
    assert t._rate_limited("temp") is True
    # ...but eligible once backoff drops the interval to 600 s.
    t._enter_backoff()
    assert t._rate_limited("temp") is False

    t._cached_temp_ts = 0.0
    with _patch_session(get=MagicMock(return_value=_realtime())):
        t.grab_temperature_and_humidity()
        settle(t)
    assert t._in_backoff is False                # a good response cleared it
    assert t.grab_temperature_and_humidity() == (71.6, 52.0)


def test_backoff_auto_clears_after_two_hours():
    _reset()
    t._enter_backoff()
    t._backoff_entered_ts = time.time() - (t._BACKOFF_AUTO_CLEAR_S + 1)
    t._temp_last_call_ts = time.time()           # still inside every interval
    assert t._rate_limited("temp") is True       # (the check that clears it)
    assert t._in_backoff is False


def test_network_error_does_not_clear_backoff():
    """Only a real 2xx clears backoff; a DNS failure must leave it set so the
    2-hour auto-clear stays in charge."""
    _reset()
    t._enter_backoff()
    with _patch_session(get=MagicMock(side_effect=t.RequestException("dns"))):
        t.grab_temperature_and_humidity()
        settle(t)
    assert t._in_backoff is True


def test_session_never_retries_a_429():
    """Retrying a 429 makes rate limiting worse — the adapter must not."""
    t._session = None
    s = t.get_session()
    retries = s.get_adapter("https://api.tomorrow.io").max_retries
    assert 429 not in retries.status_forcelist
    assert set(retries.status_forcelist) == {500, 502, 503, 504}
    t._session = None


# ══ data shapes the web mirror depends on ═══════════════════════════════════

def test_disk_cache_shapes_are_unchanged():
    _reset()
    with _patch_session(get=MagicMock(return_value=_realtime()),
                        post=MagicMock(return_value=_forecast_resp())):
        t.grab_temperature_and_humidity()
        settle(t)
        t.grab_forecast(tag="test")
        settle(t)

    with open(t._TEMP_CACHE_FILE) as f:
        obj = json.load(f)
    assert obj["data"] == [71.6, 52.0, 4]        # web/app.py indexes [0], [1], [2]
    assert obj["units"] == "imperial"
    assert obj["ts"] > 0

    with open(t._FORECAST_CACHE_FILE) as f:
        obj = json.load(f)
    assert len(obj["data"]) == 3
    assert set(obj["data"][0]["values"]) >= {
        "temperatureMin", "temperatureMax", "weatherCodeFullDay",
        "sunriseTime", "sunsetTime", "moonPhase"}

    with open(t._HOURLY_UV_CACHE_FILE) as f:
        pts = json.load(f)
    assert [u for _, u in pts] == [2.0, 8.0]     # [[epoch, uv], ...]


def test_hourly_uv_curve_still_rides_the_forecast_call():
    _reset()
    post = MagicMock(return_value=_forecast_resp(uv_points=(2.0, 8.0)))
    with _patch_session(post=post):
        t.grab_forecast(tag="test")
        settle(t)
        post.assert_called_once()                # one call feeds both
    uv = t.get_current_uv()
    assert 2.0 < uv < 8.0                        # interpolated to "now"
    assert t.get_uv_index() is None              # realtime never ran


def test_uv_getters_never_dispatch_anything():
    """The web mirror process imports this module and calls get_current_uv();
    it must never fetch (it has no business hitting Tomorrow.io)."""
    _reset()
    with _patch_session(get=MagicMock(side_effect=AssertionError("fetched!")),
                        post=MagicMock(side_effect=AssertionError("fetched!"))):
        for _ in range(5):
            assert t.get_current_uv() is None
            assert t.get_uv_index() is None
        assert t._refresh_pending is False


def test_unconfigured_key_makes_no_call():
    _reset(api_key=None)
    get = MagicMock()
    post = MagicMock()
    with _patch_session(get=get, post=post):
        assert t.grab_temperature_and_humidity() == (None, None)
        assert t.grab_forecast(tag="test") == []
        get.assert_not_called()
        post.assert_not_called()
    assert t._refresh_pending is False
    _reset()


# ══ icon warming (bonus) ════════════════════════════════════════════════════

def test_forecast_worker_warms_only_the_icons_that_forecast_uses():
    """_icon_pixels() decoded PNGs lazily on the render thread — PIL open +
    LANCZOS + a per-pixel tuple build, 338 ms on the first forecast paint. The
    background worker pre-decodes exactly the codes this forecast names (there
    are 247 files in icons/; preloading them all is not an option)."""
    import scenes.daysforecast as ds
    _reset()
    ds._ICON_CACHE.clear()

    # _icon_pixels() opens "icons/<code>.png" RELATIVE to the cwd, and icons/ is
    # a symlink into the repo root (like logos/). Fail loudly on a dangling copy
    # rather than silently caching None and asserting on the wrong thing.
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(t.__file__)))
    assert os.path.isfile(os.path.join(app_root, "icons", f"{_ICON}.png")), (
        "icons/ is missing or a dangling symlink — copy the tree with `cp -RL`")

    cwd = os.getcwd()
    os.chdir(app_root)
    try:
        with _patch_session(post=MagicMock(return_value=_forecast_resp())):
            t.grab_forecast(tag="test")
            settle(t)
    finally:
        os.chdir(cwd)

    assert _ICON in ds._ICON_CACHE, "worker did not warm the forecast's icon"
    w, h, pixels = ds._ICON_CACHE[_ICON]
    assert (w, h) == (ds.ICON_SIZE, ds.ICON_SIZE)
    assert len(pixels) == w * h
    # only what the forecast referenced — not the other 246 files
    assert set(ds._ICON_CACHE) == {_ICON}

    # and the render thread now gets it for free: no PIL work on first paint
    with patch("scenes.daysforecast.Image.open",
               side_effect=AssertionError("decoded on the render thread")):
        assert ds._icon_pixels(_ICON) is ds._ICON_CACHE[_ICON]
    ds._ICON_CACHE.clear()


def test_icon_warm_failure_never_breaks_the_forecast():
    import scenes.daysforecast as ds
    _reset()
    ds._ICON_CACHE.clear()
    escaped = []
    trimmed = []
    old_hook = threading.excepthook
    threading.excepthook = lambda args: escaped.append(args.exc_value)
    try:
        with patch("scenes.daysforecast._icon_pixels",
                   side_effect=RuntimeError("PIL exploded")), \
             patch("utilities.temperature._trim_memory",
                   side_effect=lambda: trimmed.append(1)):
            with _patch_session(post=MagicMock(return_value=_forecast_resp())):
                t.grab_forecast(tag="test")
                settle(t)
    finally:
        threading.excepthook = old_hook

    assert len(t.grab_forecast(tag="test")) == 3      # fetch survived intact
    with open(t._FORECAST_CACHE_FILE) as f:
        assert len(json.load(f)["data"]) == 3
    # ...and the worker ran to completion rather than dying mid-way: nothing
    # escaped the thread, and the post-fetch malloc trim still happened.
    assert escaped == [], f"exception escaped the worker: {escaped}"
    assert trimmed == [1]


def test_icon_warming_is_skipped_when_the_scene_is_not_loaded():
    """The web mirror process has no rgbmatrix; importing the scene module there
    would raise. The worker only reaches into it if it is ALREADY imported."""
    import sys
    _reset()
    saved = sys.modules.pop("scenes.daysforecast", None)
    try:
        with _patch_session(post=MagicMock(return_value=_forecast_resp())):
            t.grab_forecast(tag="test")
            settle(t)
        assert len(t.grab_forecast(tag="test")) == 3
        assert "scenes.daysforecast" not in sys.modules   # not imported by us
    finally:
        if saved is not None:
            sys.modules["scenes.daysforecast"] = saved


# ══ worker hygiene ══════════════════════════════════════════════════════════

def test_workers_move_off_the_render_core():
    """Every background worker must call run_off_render_core() first — the
    process is pinned to the isolated core that drives the LED refresh."""
    _reset()
    seen = []
    with patch("utilities.temperature.run_off_render_core",
               side_effect=lambda: seen.append(threading.current_thread().name)):
        with _patch_session(get=MagicMock(return_value=_realtime())):
            t.grab_temperature_and_humidity()
            settle(t)
        _open_the_gates()
        with _patch_session(post=MagicMock(return_value=_forecast_resp())):
            t.grab_forecast(tag="test")
            settle(t)
    assert len(seen) == 2
    assert all(name != threading.current_thread().name for name in seen)


def test_workers_are_daemon_threads():
    """A hung fetch must never keep the process alive on shutdown."""
    _reset()
    started = []
    real_thread = t.threading.Thread

    def record(*a, **kw):
        th = real_thread(*a, **kw)
        started.append(th)
        return th

    with patch("utilities.temperature.threading.Thread", side_effect=record):
        with _patch_session(get=MagicMock(return_value=_realtime())):
            t.grab_temperature_and_humidity()
            settle(t)
    assert started and all(th.daemon for th in started)


@pytest.fixture(autouse=True)
def _leave_the_module_clean():
    """Never leave a worker or a poisoned cache behind for the next test file."""
    yield
    deadline = time.monotonic() + 5
    while t._refresh_pending and time.monotonic() < deadline:
        time.sleep(0.001)
    _reset()
