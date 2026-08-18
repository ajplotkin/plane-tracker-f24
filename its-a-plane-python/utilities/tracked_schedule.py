"""
tracked_schedule.py — delay-aware refresh of the tracked flight's AirLabs schedule.

WHY THIS EXISTS

A flight the user is TRACKING but that has not taken off yet is invisible to the
FR24 live feed, so `overhead.py` falls back to an AirLabs /schedules lookup and
renders "Departs <dep_time> ORIGIN->DEST". That lookup used to happen exactly
once per tracked flight: the result was cached in `Overhead._tracked_schedule_cache`
and only re-fetched once the cached ARRIVAL time had passed. A DEPARTURE delay
was therefore never picked up — the panel kept showing the original time right up
until the aircraft appeared in the live feed.

This module re-fetches that schedule while the flight is still pre-departure, on
a cadence tied to how far away the departure is, and never on the caller's thread.

CADENCE

A tracked flight can be set days in advance, so a flat poll would burn credits
for hours on data that cannot have changed; conversely delays firm up in the last
few hours, which is exactly when a stale time is most annoying. Hence three tiers
(see the constants below): 3h apart beyond 12h out, hourly from 3-12h out, and
every 15 minutes inside 3h — including after a scheduled departure that has come
and gone without the aircraft showing up, which is the definition of delayed.

THREADING

`maybe_refresh()` is the getter: it returns last-good immediately and dispatches
`_background_refresh` on a worker thread when the cadence says a fetch is due.
It never performs I/O itself. The worker calls `run_off_render_core()` first (see
utilities/cpu_affinity.py — background CPU on the isolated core starves the LED
refresh thread and visibly freezes the scroll) and clears `_refresh_pending` in a
`finally`. Same shape as utilities/airport_status.py and utilities/tides.py.
"""

import logging
import threading
import time as _time
from datetime import datetime, timezone

from utilities.cpu_affinity import run_off_render_core

logger = logging.getLogger(__name__)

# --- Cadence tiers, keyed on time-to-scheduled-departure -----------------
_HORIZON_FAR_SEC = 12 * 3600     # beyond this, nothing has firmed up yet
_HORIZON_NEAR_SEC = 3 * 3600     # inside this, delays actually get published

_INTERVAL_FAR_SEC = 3 * 3600     # > 12h out
_INTERVAL_MID_SEC = 60 * 60      # 3h - 12h out
_INTERVAL_NEAR_SEC = 15 * 60     # < 3h out, and past an unfulfilled departure

# Floor: utilities/airlabs.py keeps its own 5-minute module cache, so a refresh
# inside that window returns the same dict without touching the network. Polling
# faster than the TTL spends wall time and log noise for guaranteed-zero new data.
_AIRLABS_CACHE_TTL_SEC = 300

# Statuses after which the departure can no longer be revised.
_TERMINAL_STATUSES = ("active", "landed", "cancelled")

# --- WORST-CASE CREDIT COST ---------------------------------------------
# AirLabs free tier: 1000 credits/month, 1 credit per /schedules call. Per
# tracked flight, from the moment it is set until it departs (after which the
# flight is live on FR24 and this path is not taken) or overhead.py auto-wipes it:
#
#   > 12h out .......... 8 calls per day        (24h / 3h)
#   3h - 12h out ....... 9 calls total          (9h / 1h)
#   final 3h ........... 12 calls total         (3h / 15min)
#   past its scheduled departure but still absent from the FR24 feed:
#                        4 calls/h until the auto-wipe fires 2h past arrival
#
# TYPICAL — a flight set 24h ahead:  4 + 9 + 12 = 25 credits.
    # The real bound is not the cadence, it is overhead._MAX_TRACKED_HOURS = 36:
    # any tracked flight is wiped 36h after set_ts, so a flight set a week ahead
    # never reaches the mid/near tiers (it takes <=12 far-tier calls, then is
    # wiped). Worst realistic case is a flight that goes overdue and is never
    # seen in the feed: the near tier (4/h) until the 36h cap, ~144 credits.
    # Absolute ceiling is the AirLabs TTL, 12/h * 36h = 432, unreachable by these
    # tiers. Typical 24h-ahead flight is ~25. Free tier is 1000/month; observed
    # usage before this feature was 4.

_lock = threading.Lock()            # guards _last_good / _last_fetch_ts / _refresh_pending
_refresh_lock = threading.Lock()    # serialises the background worker
_refresh_pending = False

_last_good = {}      # callsign -> schedule dict from utilities.airlabs
_last_fetch_ts = {}  # callsign -> unix ts of the last AirLabs fetch ATTEMPT


def refresh_interval(seconds_to_departure):
    """Seconds to wait between AirLabs refreshes at a given time-to-departure.

    Negative input (departure time already passed with no aircraft in the live
    feed) lands in the near tier, which is intended: that is the delay case.
    """
    if seconds_to_departure > _HORIZON_FAR_SEC:
        interval = _INTERVAL_FAR_SEC
    elif seconds_to_departure > _HORIZON_NEAR_SEC:
        interval = _INTERVAL_MID_SEC
    else:
        interval = _INTERVAL_NEAR_SEC
    return max(interval, _AIRLABS_CACHE_TTL_SEC)


def scheduled_departure_ts(sched):
    """Unix ts of the SCHEDULED departure, or None if unparseable."""
    if not sched:
        return None
    ts = sched.get("dep_time_ts")
    if ts:
        try:
            return float(ts)
        except (TypeError, ValueError):
            pass
    try:
        return datetime.strptime(
            sched.get("dep_time_utc") or "", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def effective_departure_ts(sched, delay_minutes=None):
    """Unix ts of the departure we currently EXPECT: scheduled plus any delay.

    overhead.py gates its "NOT TRACKABLE" flag on this. A flight running 2h
    late has not failed to be trackable 30 minutes after a time it was never
    going to leave at — but that is exactly what gating on the SCHEDULED time
    concludes once a delay is known.

    With no delay information (delay_minutes None or 0 — the norm on the free
    AirLabs tier) this is identical to scheduled_departure_ts(), so the
    pre-existing behaviour is untouched. Returns None when the schedule has no
    parseable departure time at all.
    """
    base = scheduled_departure_ts(sched)
    if base is None:
        return None
    try:
        return base + max(int(delay_minutes or 0), 0) * 60
    except (TypeError, ValueError):
        return base


def is_pre_departure(sched):
    """True while AirLabs could still revise this leg's departure time.

    False once the aircraft actually left (an actual departure time exists) or
    the leg reached a status that cannot change further.
    """
    if not sched:
        return False
    if (sched.get("dep_actual") or sched.get("dep_actual_utc")
            or sched.get("dep_actual_ts")):
        return False
    if (sched.get("status") or "").lower() in _TERMINAL_STATUSES:
        return False
    return True


def note_fetched(callsign, sched, now=None):
    """Record a schedule the CALLER fetched itself.

    overhead.py still does its own cold-start lookup and its arrival-expiry
    re-fetch; telling us about them stops the cadence from immediately spending
    another credit on data that was just paid for.
    """
    if not callsign or not sched:
        return
    with _lock:
        _last_good[callsign] = sched
        _last_fetch_ts[callsign] = _time.time() if now is None else now


def forget(callsign=None):
    """Drop cached state (all callsigns when None).

    Called wherever overhead.py clears `_tracked_schedule_cache` so the two
    caches stay in lockstep — otherwise a newly tracked flight would inherit the
    previous one's fetch timestamp.
    """
    with _lock:
        if callsign is None:
            _last_good.clear()
            _last_fetch_ts.clear()
        else:
            _last_good.pop(callsign, None)
            _last_fetch_ts.pop(callsign, None)


def _background_refresh(callsign, pin_dep_ts=None):
    """Re-fetch one schedule off the render core. Never raises.

    `pin_dep_ts` keeps the refresh on the SAME LEG the caller is tracking (see
    utilities/airlabs LEG PINNING) — an unpinned refresh can hand back the next
    day's leg of a daily flight number, which is how a finished flight used to
    become un-completable. A pinned lookup can also come back LEG_GONE, which
    is falsy and therefore lands in the keep-last-good branch below: deciding a
    flight is over is overhead.py's job, not this cadence's.
    """
    global _refresh_pending
    with _refresh_lock:
        try:
            run_off_render_core()   # inside try: must never wedge the flag
            from utilities.airlabs import get_flight_schedule
            fresh = get_flight_schedule(callsign, pin_dep_ts=pin_dep_ts)
            if fresh:
                with _lock:
                    _last_good[callsign] = fresh
                logger.info(
                    f"[TrackedSchedule] refreshed {callsign}: "
                    f"dep={fresh.get('dep_time')} status={fresh.get('status')}"
                )
            else:
                # Keep last-good. A transient AirLabs miss must not blank a
                # departure time that is still the best thing we know.
                logger.info(f"[TrackedSchedule] refresh for {callsign} returned nothing")
        except Exception as e:
            logger.warning(f"[TrackedSchedule] refresh failed for {callsign}: {e}")
        finally:
            with _lock:
                # Stamp the ATTEMPT, not just the success: retrying on every
                # 90s poll after a failure would turn a bounded cadence into an
                # unbounded one exactly when the API is unhappy.
                _last_fetch_ts[callsign] = _time.time()
                _refresh_pending = False


def _dispatch(callsign, pin_dep_ts=None):
    """Start the worker unless one is already in flight."""
    global _refresh_pending
    with _lock:
        if _refresh_pending:
            return
        _refresh_pending = True
    try:
        threading.Thread(target=_background_refresh,
                         args=(callsign, pin_dep_ts),
                         daemon=True).start()
    except Exception as e:
        # Thread.start() can fail under memory pressure on the 512MB Pi. Clear
        # the flag so the next poll retries instead of wedging forever.
        logger.warning(f"[TrackedSchedule] could not start refresh thread: {e}")
        with _lock:
            _refresh_pending = False


def maybe_refresh(callsign, sched, now=None, pin_dep_ts=None):
    """Return the freshest known schedule for `callsign`. NEVER BLOCKS.

    Returns last-good immediately; when the adaptive cadence says a refresh is
    due it dispatches a background worker whose result shows up on a later call.
    `sched` is the caller's current copy, used to seed the cache on first sight.
    `pin_dep_ts` pins the refresh to one leg — see _background_refresh.
    """
    if not callsign:
        return sched
    now = _time.time() if now is None else now

    with _lock:
        best = _last_good.get(callsign)
        if best is None:
            if not sched:
                return sched
            # First sight: the caller has just fetched this. Seed both the value
            # and the fetch time so the first background refresh lands a full
            # interval later rather than immediately re-buying the same data.
            _last_good[callsign] = sched
            _last_fetch_ts[callsign] = now
            return sched
        last_fetch = _last_fetch_ts.get(callsign, 0)

    if not is_pre_departure(best):
        return best

    dep_ts = scheduled_departure_ts(best)
    # Unparseable departure time: treat as imminent rather than never refreshing.
    # Still bounded — the near tier is 15 min, well above the AirLabs TTL.
    seconds_to_departure = (dep_ts - now) if dep_ts is not None else 0.0

    if (now - last_fetch) >= refresh_interval(seconds_to_departure):
        _dispatch(callsign, pin_dep_ts)

    return best
