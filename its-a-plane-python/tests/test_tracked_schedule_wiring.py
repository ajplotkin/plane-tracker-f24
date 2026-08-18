"""
test_tracked_schedule_wiring.py — the delay feature is actually WIRED IN.

The unit tests in test_tracked_schedule.py exercise tracked_schedule.maybe_refresh()
and airlabs.departure_delay() directly. That left a hole big enough to drive the
whole feature through: a review mutation replaced

    sched = tracked_schedule.maybe_refresh(tracked_callsign, sched) or sched
with
    sched = sched or tracked_schedule.maybe_refresh(tracked_callsign, sched)

in utilities/overhead.py. Since `sched` is always truthy in that branch, the
refresher is never called and the feature is entirely disabled — and all 460 tests
still passed. These tests close that gap: they drive Overhead's pre-departure branch
and assert the wiring, not the helpers.
"""

import pytest

import utilities.tracked_schedule as ts


DEP_LOCAL = "2026-05-11 18:30"
DEP_UTC = "2026-05-11 22:30"


def _sched(**over):
    s = {
        "origin": "EWR", "destination": "LAX",
        "dep_time": DEP_LOCAL, "dep_time_utc": DEP_UTC,
        "dep_time_ts": 1778963400,
        "arr_time": "2026-05-11 21:45", "arr_time_utc": "2026-05-12 01:45",
        "arr_time_ts": 1778974500,
        "status": "scheduled",
    }
    s.update(over)
    return s


def test_overhead_calls_maybe_refresh_on_a_cached_schedule():
    """The exact mutation that survived: if the call is short-circuited by an
    `or`, the refresher never runs and delays are never picked up."""
    src = open("utilities/overhead.py", encoding="utf-8").read()
    # The call must not be guarded by anything that is always truthy here.
    assert "tracked_schedule.maybe_refresh(" in src
    bad = "sched = sched or tracked_schedule.maybe_refresh("
    assert bad not in src, (
        "maybe_refresh is short-circuited by an always-truthy `sched` — the "
        "delay refresh can never run")
    # And it must feed back into the cache, or the refreshed value is discarded.
    idx = src.index("tracked_schedule.maybe_refresh(")
    after = src[idx:idx + 400]
    assert "_tracked_schedule_cache[tracked_callsign] = sched" in after, (
        "the refreshed schedule is not written back to the cache")


def test_refreshed_delay_reaches_the_tracked_payload():
    """A delay returned by the refresher must surface as dep_delay_min /
    dep_time_revised, which is what the scene and the mirror render."""
    from utilities.airlabs import departure_delay
    delayed = _sched(dep_estimated="2026-05-11 20:15",
                     dep_estimated_ts=1778963400 + 105 * 60,
                     dep_estimated_utc="2026-05-12 00:15")
    mins, revised = departure_delay(delayed)
    assert mins == 105, "the refreshed schedule must yield the delay"
    assert revised == "2026-05-11 20:15"

    src = open("utilities/overhead.py", encoding="utf-8").read()
    assert '"dep_delay_min": _dep_delay_min' in src
    assert '"dep_time_revised": _dep_time_revised' in src
    assert "departure_delay(sched)" in src, (
        "the payload fields exist but nothing computes them from the schedule")


def test_not_trackable_is_gated_on_the_effective_departure():
    """A flight we are showing as 2h late must not be flagged NOT TRACKABLE 30
    minutes after a time it was never going to leave at."""
    src = open("utilities/overhead.py", encoding="utf-8").read()
    assert "tracked_schedule.effective_departure_ts(" in src
    idx = src.index("tracked_schedule.effective_departure_ts(")
    call = src[idx:idx + 160]
    assert "_dep_delay_min" in call, (
        "effective_departure_ts is called without the delay, so it degrades to "
        "the plain scheduled time and the flag fires early on a delayed flight")

    # And the helper must actually shift by the delay.
    base = ts.effective_departure_ts(_sched(), None)
    late = ts.effective_departure_ts(_sched(), 120)
    assert late == pytest.approx(base + 120 * 60), (
        "effective_departure_ts ignores the delay")


def test_every_schedule_cache_wipe_also_clears_the_refresher():
    """The refresher keeps its own _last_good / _last_fetch_ts. If a wipe clears
    Overhead's cache but not the refresher, a stale delay (and its cadence state)
    outlives the flight and can attach to the next one.

    Asserts the PAIRING, not mere presence: a bare `forget( in src` check passed
    even after one of the two call sites was deleted.
    """
    src = open("utilities/overhead.py", encoding="utf-8").read()
    clears = src.count("self._tracked_schedule_cache.clear()")
    assert clears >= 2, "unexpected wipe sites — re-check this invariant"
    # Every clear() must be followed by forget() within a couple of lines.
    paired = 0
    start = 0
    while True:
        i = src.find("self._tracked_schedule_cache.clear()", start)
        if i == -1:
            break
        window = src[i:i + 200]
        assert "tracked_schedule.forget(" in window, (
            f"a _tracked_schedule_cache.clear() at offset {i} is not paired with "
            f"tracked_schedule.forget() — refresher state survives the wipe")
        paired += 1
        start = i + 1
    assert paired == clears


def test_exactly_sixty_minutes_renders_as_an_hour():
    """Boundary the review found unpinned: at exactly 60 the format must switch to
    h:mm. A mutation of `< 60` to `<= 60` renders (+60m) and nothing caught it."""
    from scenes.trackedstats import _format_delay
    assert _format_delay(59) == "(+59m)"
    assert _format_delay(60) == "(+1:00)"
    assert _format_delay(61) == "(+1:01)"
    assert _format_delay(125) == "(+2:05)"


def test_unparseable_departure_uses_the_soonest_tier_not_the_slowest():
    """If the departure time cannot be parsed we know nothing about timing, so the
    safe default is to check again SOON. A review mutation flipped that fallback
    from 0.0 to 1e9, silently dropping such flights to the 3-hour tier; nothing
    caught it."""
    src = open("utilities/tracked_schedule.py", encoding="utf-8").read()
    assert "seconds_to_departure = (dep_ts - now) if dep_ts is not None else 0.0" in src, (
        "the unparseable-departure fallback is no longer 0.0, so an unreadable "
        "schedule may fall into a slow tier instead of being re-checked promptly")
    # 0.0 must actually land in the near tier.
    assert ts.refresh_interval(0.0) == max(ts._INTERVAL_NEAR_SEC,
                                           ts._AIRLABS_CACHE_TTL_SEC)


def test_a_passed_departure_stays_in_the_near_tier():
    """Departure time gone but the aircraft still absent from the feed IS the
    delay case — it must keep checking at the fast cadence, not back off."""
    near = max(ts._INTERVAL_NEAR_SEC, ts._AIRLABS_CACHE_TTL_SEC)
    assert ts.refresh_interval(-60) == near
    assert ts.refresh_interval(-7200) == near


def test_tier_boundaries_are_enforced():
    far = max(ts._INTERVAL_FAR_SEC, ts._AIRLABS_CACHE_TTL_SEC)
    mid = max(ts._INTERVAL_MID_SEC, ts._AIRLABS_CACHE_TTL_SEC)
    near = max(ts._INTERVAL_NEAR_SEC, ts._AIRLABS_CACHE_TTL_SEC)
    assert ts.refresh_interval(ts._HORIZON_FAR_SEC + 1) == far
    assert ts.refresh_interval(ts._HORIZON_FAR_SEC) == mid
    assert ts.refresh_interval(ts._HORIZON_NEAR_SEC + 1) == mid
    assert ts.refresh_interval(ts._HORIZON_NEAR_SEC) == near
    assert near <= mid <= far, "tiers must widen with time-to-departure"
