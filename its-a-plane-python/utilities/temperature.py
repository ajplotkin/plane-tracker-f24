"""
Tomorrow.io weather API wrapper with built-in rate limiting.

Free tier: 25 requests/hour (one every ~2.4 minutes).
We enforce a minimum 3-minute gap between ALL API calls to stay safe.

NON-BLOCKING CONTRACT (2026-08-16)
----------------------------------
Every public getter here — grab_temperature_and_humidity(), grab_forecast(),
get_uv_index(), get_current_uv() — returns the LAST GOOD value immediately and
never touches the network on the calling thread. Both HTTP calls (a GET with a
(5, 20) timeout and a POST with the same, each behind a urllib3 Retry that can
stretch a bad round-trip well past a minute) used to run on the 10 FPS render
thread, so any slow round-trip was a visible scroll freeze. They now run in a
background worker; the getters only decide whether to dispatch one.

Same shape as airport_status.py / rain.py / tides.py: module-level _refresh_lock + _refresh_pending, a
_dispatch() helper, a _background_* worker that calls run_off_render_core()
first and clears the pending flag in a finally.
"""
from datetime import datetime, timedelta
import sys
import time
import logging
import socket
import threading

from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry
from utilities.cpu_affinity import run_off_render_core
import gc as _gc
import ctypes as _ctypes

# Force glibc to return freed arenas to the OS after each API call. Without
# this, CPython's pymalloc holds freed-but-not-empty arenas indefinitely —
# on a 512MB Pi 3A+, repeated HTTP+JSON parse cycles (every 15 min) cause
# RSS to ratchet up via fragmentation even without a true leak.
try:
    _libc = _ctypes.CDLL("libc.so.6")
    def _trim_memory():
        _gc.collect()
        _libc.malloc_trim(0)
except (OSError, AttributeError):
    def _trim_memory():
        _gc.collect()

try:
    from utilities.api_usage import log_call as _log_api
except ImportError:
    _log_api = lambda source: None

# Attempt to load config data
try:
    from config import TOMORROW_API_KEY
    from config import TEMPERATURE_UNITS
    from config import FORECAST_DAYS
    from config import TEMPERATURE_LOCATION
except (ModuleNotFoundError, NameError, ImportError):
    TOMORROW_API_KEY = None
    TEMPERATURE_UNITS = "metric"
    FORECAST_DAYS = 3
    TEMPERATURE_LOCATION = ""

if TEMPERATURE_UNITS not in ("metric", "imperial"):
    TEMPERATURE_UNITS = "metric"

logger = logging.getLogger(__name__)

_warned = set()


def _warn_once(key, msg):
    """Log a config-problem warning once per process.

    These getters are now polled EVERY FRAME while nothing is cached (the scenes
    stopped gating on a retry timer so async data is never stranded), so an
    unconfigured key would otherwise emit ~3 warnings/sec — ~260k journald lines a
    day on a device that was simply deployed before its key was entered.
    """
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg)

# ─── Rate Limiter ────────────────────────────────────────────────────────────
# Separate rate limiters for temperature and forecast so they don't block each other.
# Normal mode: 1 API call per 30 minutes per endpoint.
# Backoff mode (after 429): 1 call every 10 minutes until success.
# Backoff auto-clears after 2 hours regardless.
_NORMAL_INTERVAL_S = 1800   # 30 min between calls per endpoint
_BACKOFF_INTERVAL_S = 600   # 10 minutes when rate-limited
_BACKOFF_AUTO_CLEAR_S = 7200  # Auto-clear backoff after 2 hours

_temp_last_call_ts = 0.0
_fc_last_call_ts = 0.0
_in_backoff = False
_backoff_entered_ts = 0.0
_rate_lock = threading.Lock()


def _rate_limited(endpoint: str = "temp") -> bool:
    """Return True if we should skip this API call due to rate limiting."""
    global _in_backoff, _backoff_entered_ts
    with _rate_lock:
        # Auto-clear backoff after 2 hours
        if _in_backoff and (time.time() - _backoff_entered_ts) > _BACKOFF_AUTO_CLEAR_S:
            _in_backoff = False
            logger.info("Tomorrow.io: backoff auto-cleared after 2 hours")

        last_ts = _temp_last_call_ts if endpoint == "temp" else _fc_last_call_ts
        elapsed = time.time() - last_ts
        interval = _BACKOFF_INTERVAL_S if _in_backoff else _NORMAL_INTERVAL_S
        if elapsed < interval:
            return True
        return False


def _record_call(endpoint: str = "temp"):
    """Record that an API call was just made."""
    global _temp_last_call_ts, _fc_last_call_ts
    with _rate_lock:
        if endpoint == "temp":
            _temp_last_call_ts = time.time()
        else:
            _fc_last_call_ts = time.time()


def _enter_backoff():
    """Enter backoff mode after receiving 429."""
    global _in_backoff, _backoff_entered_ts
    with _rate_lock:
        _in_backoff = True
        _backoff_entered_ts = time.time()
    logger.warning("Tomorrow.io: entering backoff mode (retry every 10 min)")


def _exit_backoff():
    """Exit backoff mode after a successful response."""
    global _in_backoff
    with _rate_lock:
        if _in_backoff:
            _in_backoff = False
            logger.info("Tomorrow.io: backoff cleared, resuming normal interval")


# ─── Background refresh plumbing ─────────────────────────────────────────────
# ONE in-flight worker for the WHOLE module — the realtime (temp/humidity/UV)
# and forecast fetches share the slot, like tides.py does for
# their two getters each. Justification:
#   * They hit the SAME host through the SAME Session (pool_maxsize=2), and the
#     free tier's 25 req/hour is an ACCOUNT limit, not a per-endpoint one — so
#     running them concurrently buys no throughput, it only widens the peak.
#   * Their poll intervals are 900 s (forecast TTL) / 3600 s (realtime TTL) with
#     a 1800 s per-endpoint rate limit on top, while a worker lives seconds. The
#     odds of them wanting the slot in the same instant are negligible, and when
#     it happens the loser returns its last-good value WITHOUT consuming its
#     dispatch gate and simply re-tries on the next render frame (≤ 1 s later).
#   * On the 512MB Pi this bounds the module to one TLS handshake + one JSON
#     parse at a time — the very allocation burst _trim_memory() exists to mop
#     up. Two at once is exactly the peak that made RSS ratchet.
#   * UV needs no slot of its own: get_uv_index() reads the realtime tuple and
#     get_current_uv() interpolates the hourly curve that rides the forecast
#     call. Neither ever fetches.
_refresh_lock = threading.Lock()
# NOTE: the check-then-set of _refresh_pending in the getters is deliberately
# NOT lock-protected. It is safe only because every getter here has exactly ONE
# caller thread: Animator.play() runs all scenes sequentially in one loop, and
# the web mirror is a SEPARATE PROCESS (and only calls get_current_uv(), which
# never dispatches). If a second caller thread is ever added, two workers could
# briefly overlap (serialized by _refresh_lock, so bounded at 2 -- not unbounded
# growth), but the "one in-flight" invariant becomes soft.
_refresh_pending = False

# Dispatch gates — SEPARATE from the rate limiter above, and needed because of
# it. _record_call() fires only when Tomorrow.io actually ANSWERED (200 or 429);
# a DNS failure / connect timeout deliberately leaves it alone so a transient
# outage doesn't burn a 30-minute slot. That was safe while the fetch blocked
# (the caller waited out the timeout), but a getter that returns instantly and
# is called at 1 Hz by the render loop would re-dispatch a worker on EVERY frame
# for as long as the network is down. These timestamps are advanced BEFORE the
# dispatch (and restored if Thread.start() fails), which bounds a failing
# endpoint to one attempt per _DISPATCH_RETRY_S. Same fix as rain.py/tides.py.
_DISPATCH_RETRY_S = 300     # 5 min between dispatch attempts that never answered
_temp_dispatch_ts = 0.0
_fc_dispatch_ts = 0.0


def _dispatch(target, args):
    """Start a background worker if none is in flight. Returns True if it started.

    Mirrors airport_status.py: the pending flag is cleared again if Thread.start()
    itself fails (OOM on the 512MB Pi) so the module can never wedge.
    """
    global _refresh_pending
    if _refresh_pending:
        return False
    _refresh_pending = True
    try:
        threading.Thread(target=target, args=args, daemon=True).start()
        return True
    except Exception:
        _refresh_pending = False   # start() failed (OOM) — retry next call
        return False


# ─── DNS helper ──────────────────────────────────────────────────────────────

def is_dns_error(exc: Exception) -> bool:
    cause = exc
    while cause:
        if isinstance(cause, socket.gaierror):
            return True
        cause = cause.__cause__
    return False


# ─── HTTP Session (shared, with retries on server errors only) ───────────────
_session = None


def get_session() -> Session:
    global _session
    if _session is None:
        _session = Session()

        retries = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=3,
            allowed_methods=["GET", "POST"],
            # Do NOT retry on 429 — that makes rate limiting worse
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=2,
            pool_maxsize=2,
        )

        _session.mount("https://", adapter)
        _session.mount("http://", adapter)

    return _session


# ─── API URL ─────────────────────────────────────────────────────────────────
TOMORROW_API_URL = "https://api.tomorrow.io/v4"


# ─── Persistent File Cache ───────────────────────────────────────────────────
# Survives reboots — prevents death-spiral when Tomorrow.io 429s on startup.
import os as _os
import json as _json

_CACHE_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), ".cache")
_os.makedirs(_CACHE_DIR, exist_ok=True)
_TEMP_CACHE_FILE = _os.path.join(_CACHE_DIR, "temperature.json")
_FORECAST_CACHE_FILE = _os.path.join(_CACHE_DIR, "forecast.json")


def _load_file_cache(path, units=None):
    """Load cached data from file. Returns (data, timestamp) or (None, 0).
    If units is provided, treats mismatched units as a cache miss."""
    try:
        with open(path, "r") as f:
            obj = _json.load(f)
            if units and obj.get("units") and obj["units"] != units:
                logger.info(f"Cache units mismatch ({obj['units']} != {units}), ignoring {path}")
                return None, 0
            return obj.get("data"), obj.get("ts", 0)
    except (FileNotFoundError, _json.JSONDecodeError, KeyError):
        return None, 0


def _save_file_cache(path, data, units=None):
    """Save data + timestamp (+ optional units) to file cache."""
    try:
        obj = {"data": data, "ts": time.time()}
        if units:
            obj["units"] = units
        # Atomic (tmp + os.replace) — the web dashboard reads temperature.json /
        # forecast.json cross-process; a plain open('w') truncates first and a
        # concurrent read got a partial doc. (Matches the hourly_uv writer below.)
        tmp = f"{path}.tmp.{_os.getpid()}"
        with open(tmp, "w") as f:
            _json.dump(obj, f)
        _os.replace(tmp, path)
    except (PermissionError, OSError) as e:
        logger.warning(f"Cannot write cache {path}: {e}")


def _invalidate_on_units_change():
    """Delete cache files if units changed since last save.
    Concept from c0wsaysmoo/plane-tracker-rgb-pi."""
    for path in (_TEMP_CACHE_FILE, _FORECAST_CACHE_FILE):
        try:
            with open(path, "r") as f:
                obj = _json.load(f)
            if obj.get("units") and obj["units"] != TEMPERATURE_UNITS:
                _os.remove(path)
                logger.info(f"Invalidated {path}: units changed to {TEMPERATURE_UNITS}")
        except (FileNotFoundError, _json.JSONDecodeError, KeyError):
            pass


_invalidate_on_units_change()


# ─── Temperature & Humidity ──────────────────────────────────────────────────
_cached_temp = None
_cached_temp_ts = 0.0
_TEMP_CACHE_TTL = 3600  # 1 hour

# Load persistent cache on startup
_startup_temp, _startup_temp_ts = _load_file_cache(_TEMP_CACHE_FILE, units=TEMPERATURE_UNITS)
if _startup_temp and (time.time() - _startup_temp_ts) < _TEMP_CACHE_TTL * 2:
    _cached_temp = tuple(_startup_temp) if isinstance(_startup_temp, list) else _startup_temp
    _cached_temp_ts = _startup_temp_ts
    logger.info(f"Loaded cached temperature from file: {_cached_temp}")


def _last_good_temp():
    """The last good (temperature, humidity), or (None, None) if never fetched."""
    return (_cached_temp[0], _cached_temp[1]) if _cached_temp else (None, None)


def _fetch_temperature():
    """The blocking realtime fetch. Runs ONLY on a background worker thread.

    Byte-for-byte the old synchronous body of grab_temperature_and_humidity()
    below the rate-limit check, with the `return cached` lines replaced by bare
    returns — the caller now reads the module cache directly. Every semantic is
    preserved: 429 records the call and enters backoff (without clobbering the
    cache), a good response records the call, clears backoff, logs API usage,
    writes the disk cache and trims malloc arenas, and a network error leaves
    backoff alone for the 2-hour auto-clear.
    """
    global _cached_temp, _cached_temp_ts

    try:
        s = get_session()
        request = s.get(
            f"{TOMORROW_API_URL}/weather/realtime",
            params={
                "location": TEMPERATURE_LOCATION,
                "units": TEMPERATURE_UNITS,
                "apikey": TOMORROW_API_KEY
            },
            timeout=(5, 20)
        )

        if request.status_code == 429:
            _record_call("temp")
            _enter_backoff()
            return

        request.raise_for_status()
        _record_call("temp")
        _exit_backoff()
        _log_api("tomorrow_io")

        data = request.json().get("data", {}).get("values", {})
        temperature = data.get("temperature")
        humidity = data.get("humidity")
        uv_index = data.get("uvIndex")

        if temperature is None or humidity is None:
            logger.error("Incomplete data from Tomorrow.io API")
            return

        _cached_temp = (temperature, humidity, uv_index)
        _cached_temp_ts = time.time()
        _save_file_cache(_TEMP_CACHE_FILE, [temperature, humidity, uv_index], units=TEMPERATURE_UNITS)
        _trim_memory()

    except (RequestException, ValueError) as e:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        # Don't clear backoff on network errors — let auto-clear (2hr) handle it
        if is_dns_error(e):
            logger.error(
                f"[{timestamp}] DNS failure resolving api.tomorrow.io - will retry"
            )
        else:
            logger.error(
                f"[{timestamp}] Temperature request failed: {e}"
            )


def _background_temperature():
    """Realtime fetch on a background thread so the render loop never waits."""
    global _refresh_pending
    with _refresh_lock:
        try:
            run_off_render_core()   # inside try: must never wedge _refresh_pending
            _fetch_temperature()
        finally:
            _refresh_pending = False


def grab_temperature_and_humidity():
    """
    Return the current (temperature, humidity), or (None, None) before the first
    successful fetch.

    NEVER blocks on the network. A stale cache dispatches a background refresh
    and this returns the last good pair immediately. The cache TTL and the
    per-endpoint rate limiter are unchanged; the extra dispatch gate only bounds
    how often a fetch that never got an answer may be re-attempted.
    """
    global _temp_dispatch_ts

    if not TOMORROW_API_KEY:
        _warn_once("temp_key", "TOMORROW_API_KEY not set — skipping temperature fetch")
        return None, None

    # Return cache if still fresh
    if _cached_temp and (time.time() - _cached_temp_ts) < _TEMP_CACHE_TTL:
        return _cached_temp[0], _cached_temp[1]

    # Rate limit check
    if _rate_limited("temp"):
        logger.debug("Rate limit: skipping temperature API call, using cache")
        return _last_good_temp()

    now = time.time()
    if (now - _temp_dispatch_ts) < _DISPATCH_RETRY_S:
        return _last_good_temp()        # a recent attempt never answered
    if _refresh_pending:
        return _last_good_temp()        # in flight — gate NOT consumed

    prev_ts = _temp_dispatch_ts
    _temp_dispatch_ts = now             # advance BEFORE dispatch (anti-hammer)
    if not _dispatch(_background_temperature, ()):
        _temp_dispatch_ts = prev_ts     # start() failed — retry next call
    return _last_good_temp()


def get_uv_index():
    """Return the cached UV index from the last realtime fetch, or None."""
    if _cached_temp and len(_cached_temp) >= 3:
        return _cached_temp[2]
    return None


# ─── Forecast ────────────────────────────────────────────────────────────────
_cached_forecast = None
_cached_forecast_ts = 0.0
_FORECAST_CACHE_TTL = 900  # 15 min — refreshes the hourly UV curve 4x/hr so
# get_current_uv() tracks Tomorrow.io's own model updates sooner instead of
# leaning on interpolation between stale hourly points. Well within the free
# tier (worst case, the shared lauri/mari key: 240 calls/day of 500 limit).
# NOTE (2026-07-07): ernie (Pi 3 A+, 512MB) showed swap pressure/display
# freezing the same day this shipped. Code review found no accumulation bug
# in this code path — pending a full memory investigation on Friday.

# Load persistent forecast cache on startup
_startup_fc, _startup_fc_ts = _load_file_cache(_FORECAST_CACHE_FILE, units=TEMPERATURE_UNITS)
if _startup_fc and (time.time() - _startup_fc_ts) < _FORECAST_CACHE_TTL * 2:
    _cached_forecast = _startup_fc
    _cached_forecast_ts = _startup_fc_ts
    logger.info(f"Loaded cached forecast from file ({len(_startup_fc)} intervals)")


# ─── Hourly UV curve (rides the forecast call; interpolated to "now") ─────────
# The realtime uvIndex is a single snapshot, rate-limited to every 30 min, so it
# lags during the fast morning climb. The forecast's hourly uvIndex is a curve
# for the whole day, so indexing it to the current time gives a "current" UV
# that never goes stale — at NO extra API cost (same /timelines call). It stays
# cloud-adjusted (Tomorrow.io models cloud cover per hour).
_HOURLY_UV_CACHE_FILE = _os.path.join(_CACHE_DIR, "hourly_uv.json")
_hourly_file_mtime = 0.0        # read-side mtime cache (web app reads per request)
_hourly_file_cache = []


def _read_hourly_file():
    """Parse hourly_uv.json, re-reading only when its mtime changes. The web app
    (which never fetches) calls this per /api/display-state request, so the parse
    is cached; the file is rewritten only ~every 30 min by the display process."""
    global _hourly_file_mtime, _hourly_file_cache
    try:
        m = _os.path.getmtime(_HOURLY_UV_CACHE_FILE)
    except OSError:
        return []
    if m != _hourly_file_mtime:
        try:
            with open(_HOURLY_UV_CACHE_FILE) as f:
                _hourly_file_cache = sorted((float(t), float(u))
                                            for t, u in _json.load(f))
            _hourly_file_mtime = m
        except Exception:
            return []
    return _hourly_file_cache


# sorted [(epoch_seconds, uvIndex), ...] — populated in-process by the fetcher.
_cached_hourly_uv = _read_hourly_file()


def _iso_to_epoch(s):
    """ISO8601 with offset (2026-07-06T11:00:00-04:00) -> epoch seconds."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _update_hourly_uv(timelines):
    """Cache the hourly uvIndex curve from a /timelines response (memory + disk).
    Best-effort: never raises into the forecast fetch."""
    global _cached_hourly_uv
    try:
        hourly = next((t for t in timelines if t.get("timestep") == "1h"), None)
        if not hourly:
            return
        pts = []
        for iv in hourly.get("intervals", []):
            uv = iv.get("values", {}).get("uvIndex")
            ep = _iso_to_epoch(iv.get("startTime", ""))
            if uv is not None and ep is not None:
                pts.append((ep, float(uv)))
        if not pts:
            return
        pts.sort()
        _cached_hourly_uv = pts
        try:
            _os.makedirs(_CACHE_DIR, exist_ok=True)
            tmp = _HOURLY_UV_CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                _json.dump([[t, u] for t, u in pts], f)
            _os.replace(tmp, _HOURLY_UV_CACHE_FILE)   # atomic — no torn reads
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"hourly UV parse skipped: {e}")


def _hourly_uv_points():
    """In-memory curve (display process, which fetches) or the shared file (web
    app process, which only reads — via the mtime-cached reader)."""
    return _cached_hourly_uv if _cached_hourly_uv else _read_hourly_file()


def get_current_uv():
    """Current UV interpolated from the hourly forecast curve indexed to now, so
    it doesn't lag the 30-min realtime snapshot. Falls back to the realtime
    uvIndex, then None."""
    pts = _hourly_uv_points()
    if pts:
        now = time.time()
        if now <= pts[0][0]:
            return pts[0][1]
        if now >= pts[-1][0]:
            return pts[-1][1]
        for i in range(len(pts) - 1):
            t0, u0 = pts[i]
            t1, u1 = pts[i + 1]
            if t0 <= now <= t1:
                return u1 if t1 == t0 else u0 + (now - t0) / (t1 - t0) * (u1 - u0)
    return get_uv_index()


def _warm_forecast_icons(intervals):
    """Decode the weather icons THIS forecast references, on the worker thread.

    scenes/daysforecast._icon_pixels() decodes lazily on first paint: PIL open +
    LANCZOS thumbnail + a per-pixel tuple build, measured at 338 ms for the three
    icons of a first forecast render — 3+ dropped frames on a 10 FPS render loop.
    Warming them here moves that cost onto this worker, which is already off the
    render core.

    This runs AFTER the forecast has been cached (so a warm failure can never
    cost us the fetch), which leaves a small race: the render thread can pick up
    the new forecast and reach an icon before we have warmed it. That is not a
    correctness problem — it is exactly the old behaviour, one decode on the
    render thread — and it only costs the very first paint after a fetch, so in
    the common case the warm wins and the paint is free.

    There are 247 files in icons/, so this deliberately warms only the handful of
    weatherCodeFullDay codes the forecast actually names.

    THREAD SAFETY: _ICON_CACHE is a plain dict keyed by the icon code, and
    _icon_pixels() is `if icon in cache: return cache[icon]` / build / assign.
    The membership test and the store are single bytecodes against a builtin
    dict with str/int keys — no Python-level __hash__/__eq__ that could yield the
    GIL mid-operation — so no torn read is possible. The values are immutable
    tuples (or None) built completely before assignment, so a reader either
    misses the key and rebuilds it itself (a wasted decode, the worst case of the
    race above) or gets a finished entry. No lock needed, and the scene needs no
    change.

    Best-effort in every direction: it only reaches into the scene module if that
    module is ALREADY imported (`sys.modules`), so importing it here can never
    drag rgbmatrix into the web-mirror process or create an import cycle with
    scenes/daysforecast.py, and any failure is swallowed.
    """
    try:
        scene = sys.modules.get("scenes.daysforecast")
        if scene is None:
            return                       # display process not running this scene
        seen = set()
        for day in intervals[:6]:        # the scene shows 3; a couple spare
            try:
                icon = day["values"]["weatherCodeFullDay"]
            except (KeyError, TypeError):
                continue
            if icon in seen or icon in scene._ICON_CACHE:
                continue
            seen.add(icon)
            scene._icon_pixels(icon)
        if seen:
            logger.debug(f"[Forecast] warmed {len(seen)} weather icon(s)")
    except Exception as e:                # must NEVER break the fetch
        logger.debug(f"[Forecast] icon warm skipped: {e}")


def _last_good_forecast():
    """The last good interval list, or [] if never fetched."""
    return _cached_forecast if _cached_forecast else []


def _fetch_forecast(tag):
    """The blocking forecast fetch. Runs ONLY on a background worker thread.

    The old synchronous body of grab_forecast() below the rate-limit check, with
    the `return cached` lines replaced by bare returns. 429 handling, backoff,
    API-usage logging, the hourly UV curve harvest, the disk cache write and the
    malloc trim are all unchanged.
    """
    global _cached_forecast, _cached_forecast_ts

    dt = datetime.now()

    try:
        s = get_session()
        resp = s.post(
            f"{TOMORROW_API_URL}/timelines",
            headers={
                "Accept-Encoding": "gzip",
                "accept": "application/json",
                "content-type": "application/json"
            },
            params={"apikey": TOMORROW_API_KEY},
            json={
                "location": TEMPERATURE_LOCATION,
                "units": TEMPERATURE_UNITS,
                "timezone": "auto",
                "dailyStartHour": 6,
                "fields": [
                    "temperatureMin",
                    "temperatureMax",
                    "weatherCodeFullDay",
                    "sunriseTime",
                    "sunsetTime",
                    "moonPhase",
                    "uvIndex"
                ],
                # "1h" rides the SAME call (no extra API cost) to get an hourly
                # UV curve; get_current_uv() interpolates it to "now" so the UV
                # chip never lags like the 30-min realtime snapshot. 1h is
                # free-tier-supported; sub-hourly (5/15/30m) is paid nowcast.
                "timesteps": ["1d", "1h"],
                "endTime": (dt + timedelta(days=int(FORECAST_DAYS))).isoformat(),
            },
            timeout=(5, 20)
        )

        if resp.status_code == 429:
            _record_call("forecast")
            _enter_backoff()
            return

        resp.raise_for_status()
        _record_call("forecast")
        _exit_backoff()
        _log_api("tomorrow_io")

        data = resp.json().get("data", {})
        timelines = data.get("timelines", [])
        if not timelines:
            logger.error(f"[Forecast:{tag}] No timelines returned from API")
            return

        # Two timesteps now — find the daily one explicitly (response order is
        # not guaranteed) and harvest the hourly UV curve from the "1h" one.
        daily_tl = next((t for t in timelines if t.get("timestep") == "1d"), None)
        _update_hourly_uv(timelines)
        intervals = (daily_tl or timelines[0]).get("intervals", [])
        if not intervals:
            logger.error(f"[Forecast:{tag}] Timelines returned but no intervals")
            return

        _cached_forecast = intervals
        _cached_forecast_ts = time.time()
        _save_file_cache(_FORECAST_CACHE_FILE, intervals, units=TEMPERATURE_UNITS)
        # Decode this forecast's weather icons here rather than on the render
        # thread's first paint (see _warm_forecast_icons). After the cache store,
        # so a warm failure can never cost us the forecast.
        _warm_forecast_icons(intervals)
        _trim_memory()

    except RequestException as e:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        # Don't clear backoff on network errors — let auto-clear (2hr) handle it
        if is_dns_error(e):
            logger.error(
                f"[{timestamp}] [Forecast:{tag}] DNS failure resolving api.tomorrow.io - will retry"
            )
        else:
            logger.error(
                f"[{timestamp}] [Forecast:{tag}] API request failed: {e}"
            )

    except KeyError as e:
        logger.error(f"[Forecast:{tag}] Unexpected data format: {e}")


def _background_forecast(tag):
    """Forecast fetch on a background thread so the render loop never waits."""
    global _refresh_pending
    with _refresh_lock:
        try:
            run_off_render_core()   # inside try: must never wedge _refresh_pending
            _fetch_forecast(tag)
        finally:
            _refresh_pending = False


def grab_forecast(tag="unknown"):
    """
    Return the daily forecast intervals, or [] before the first successful fetch.

    NEVER blocks on the network. A stale cache dispatches a background refresh
    and this returns the last good list immediately. The returned shape is
    unchanged (the raw Tomorrow.io "1d" intervals), as is the .cache/forecast.json
    the web mirror reads.
    """
    global _fc_dispatch_ts

    if not TOMORROW_API_KEY:
        _warn_once("forecast_key", "TOMORROW_API_KEY not set — skipping forecast fetch")
        return []

    # Return cache if still fresh
    if _cached_forecast and (time.time() - _cached_forecast_ts) < _FORECAST_CACHE_TTL:
        return _cached_forecast

    # Rate limit check
    if _rate_limited("forecast"):
        logger.debug(f"[Forecast:{tag}] Rate limit: skipping API call, using cache")
        return _last_good_forecast()

    now = time.time()
    if (now - _fc_dispatch_ts) < _DISPATCH_RETRY_S:
        return _last_good_forecast()    # a recent attempt never answered
    if _refresh_pending:
        return _last_good_forecast()    # in flight — gate NOT consumed

    prev_ts = _fc_dispatch_ts
    _fc_dispatch_ts = now               # advance BEFORE dispatch (anti-hammer)
    if not _dispatch(_background_forecast, (tag,)):
        _fc_dispatch_ts = prev_ts       # start() failed — retry next call
    return _last_good_forecast()
