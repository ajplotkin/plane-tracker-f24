"""Delay-aware timing for a TRACKED flight that has not departed yet.

A tracked flight that has not taken off is invisible to the FR24 live feed, so
overhead.py falls back to an AirLabs /schedules lookup. That lookup used to
happen ONCE per flight — cached in Overhead._tracked_schedule_cache and only
re-fetched after the cached ARRIVAL time had passed — so a DEPARTURE delay was
never picked up and the panel kept showing the original time.

These tests cover the three pieces of the fix:
  * utilities/airlabs.departure_delay  — parsing whatever delay fields AirLabs
    actually sent (all of them are optional; see the module comment on the free
    tier), and deriving the revised departure time.
  * utilities/tracked_schedule        — the adaptive refresh cadence and the
    non-blocking background fetch contract.
  * scenes/trackedstats._build_stats  — a delay visibly changing the LED line.

Nothing here sleeps to advance time: the cadence tests drive a virtual clock
through maybe_refresh(now=...). The one wall-clock assertion is deliberate — it
is the test that fails if the getter is ever made synchronous again.
"""
import threading
import time
from unittest.mock import patch

from tests.conftest import settle
from utilities import airlabs, tracked_schedule


# Scheduled departure: 18:30 at EWR (UTC-4 in May) == 22:30 UTC.
# The three representations MUST agree — an incoherent fixture would let code
# that mixes the timestamp and the string pairs pass. test_fixture_is_coherent
# pins that.
_DEP_LOCAL = "2026-05-11 18:30"
_DEP_UTC = "2026-05-11 22:30"
_DEP_TS = 1778538600            # == _DEP_UTC as a unix timestamp


# A minimal pre-departure schedule as get_flight_schedule() returns it.
def _sched(**overrides):
    base = {
        "origin": "EWR",
        "destination": "LAX",
        "dep_time": _DEP_LOCAL,
        "dep_time_utc": _DEP_UTC,
        "dep_time_ts": _DEP_TS,
        "arr_time": "2026-05-11 21:45",
        "arr_time_utc": "2026-05-12 01:45",
        "arr_estimated_utc": "",
        "arr_actual_utc": "",
        "dep_estimated": "",
        "dep_estimated_utc": "",
        "dep_estimated_ts": None,
        "dep_actual": "",
        "dep_actual_utc": "",
        "dep_actual_ts": None,
        "dep_delayed": None,
        "delayed": None,
        "status": "scheduled",
        "flight_number": "UA353",
    }
    base.update(overrides)
    return base


def _reset():
    tracked_schedule.forget()
    tracked_schedule._refresh_pending = False


# ---------------------------------------------------------------------------
# departure_delay — parse only fields that genuinely exist
# ---------------------------------------------------------------------------

def test_fixture_is_coherent():
    """dep_time / dep_time_utc / dep_time_ts must describe the same instant.

    They did not at first, and the 2h skew silently weakened every test that
    could reach the value by more than one route.
    """
    from datetime import datetime, timezone
    assert datetime.strptime(_DEP_UTC, "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc).timestamp() == _DEP_TS
    # EWR is UTC-4 in May
    local = datetime.strptime(_DEP_LOCAL, "%Y-%m-%d %H:%M")
    utc = datetime.strptime(_DEP_UTC, "%Y-%m-%d %H:%M")
    assert (utc - local).total_seconds() == 4 * 3600


class TestDepartureDelay:

    def test_no_delay_fields_returns_unknown(self):
        """None (unknown) is distinct from 0 (known on time) — callers render
        the plain scheduled time for None."""
        assert airlabs.departure_delay(_sched()) == (None, "")
        assert airlabs.departure_delay(None) == (None, "")
        assert airlabs.departure_delay({}) == (None, "")

    def test_estimated_timestamps(self):
        mins, revised = airlabs.departure_delay(_sched(
            dep_estimated_ts=_DEP_TS + 105 * 60,
            dep_estimated="2026-05-11 20:15",
        ))
        assert mins == 105
        assert revised == "2026-05-11 20:15"

    def test_estimated_utc_strings_when_no_timestamps(self):
        """dep_estimated_ts absent — the UTC string pair still yields the delay."""
        mins, revised = airlabs.departure_delay(_sched(
            dep_time_ts=None,
            dep_estimated_utc="2026-05-11 23:20",
            dep_estimated="2026-05-11 19:20",
        ))
        assert mins == 50
        assert revised == "2026-05-11 19:20"

    def test_utc_pair_alone_is_enough(self):
        """Only dep_estimated_utc present — no timestamps and no local revised
        time. The UTC pair must carry both the delay and (by derivation) the
        revised local time, so dropping that path cannot go unnoticed."""
        mins, revised = airlabs.departure_delay(_sched(
            dep_time_ts=None,
            dep_estimated_utc="2026-05-11 23:20",
        ))
        assert mins == 50
        assert revised == "2026-05-11 19:20"   # 18:30 local + 50 min

    def test_actual_beats_estimated(self):
        """Different magnitudes so picking the wrong source cannot pass."""
        mins, revised = airlabs.departure_delay(_sched(
            dep_estimated_ts=_DEP_TS + 30 * 60,
            dep_estimated="2026-05-11 19:00",
            dep_actual_ts=_DEP_TS + 75 * 60,
            dep_actual="2026-05-11 19:45",
        ))
        assert mins == 75
        assert revised == "2026-05-11 19:45"

    def test_dep_delayed_minutes_only(self):
        """Only a duration — the revised time is derived from the schedule."""
        mins, revised = airlabs.departure_delay(_sched(dep_delayed=40))
        assert mins == 40
        assert revised == "2026-05-11 19:10"

    def test_delayed_field_is_never_used_for_departure(self):
        """`delayed` is the ARRIVAL delay despite the name, so it must not leak
        into the departure figure.

        Verified against a live AirLabs /schedules call (EWR, 2026-08-17, 100
        rows): `delayed` equalled `arr_delayed` in every row, never the departure
        delay. Real example — UA1202 scheduled 17:55, actually departed 17:50
        (five minutes EARLY), dep_delayed None, arr_delayed 40, delayed 40. Using
        `delayed` would announce "Departs +40" for a flight leaving early.
        """
        mins, revised = airlabs.departure_delay(_sched(delayed=25))
        assert mins is None, "arrival delay must not be reported as a departure delay"
        assert revised == ""

    def test_arr_delayed_is_never_used_for_departure(self):
        mins, _ = airlabs.departure_delay(_sched(arr_delayed=55))
        assert mins is None

    def test_dep_delayed_is_used_and_unaffected_by_arrival_fields(self):
        mins, _ = airlabs.departure_delay(_sched(dep_delayed=40, delayed=90,
                                                 arr_delayed=90))
        assert mins == 40

    def test_explicit_time_preferred_over_duration_field(self):
        """A revised TIME is more precise than a duration; magnitudes differ so
        a fallthrough to dep_delayed would fail."""
        mins, revised = airlabs.departure_delay(_sched(
            dep_estimated_ts=_DEP_TS + 20 * 60,
            dep_estimated="2026-05-11 18:50",
            dep_delayed=200,
        ))
        assert mins == 20
        assert revised == "2026-05-11 18:50"

    def test_early_departure_clamps_to_zero_not_negative(self):
        mins, _ = airlabs.departure_delay(_sched(
            dep_estimated_ts=_DEP_TS - 10 * 60,
            dep_estimated="2026-05-11 18:20",
        ))
        assert mins == 0

    def test_derived_revised_time_crosses_midnight(self):
        mins, revised = airlabs.departure_delay(_sched(
            dep_time="2026-05-11 23:30", dep_delayed=45))
        assert mins == 45
        assert revised == "2026-05-12 00:15"

    def test_garbage_fields_do_not_raise(self):
        assert airlabs.departure_delay(_sched(dep_delayed="soon")) == (None, "")
        assert airlabs.departure_delay(
            _sched(dep_estimated_utc="not-a-date")) == (None, "")

    def test_get_flight_schedule_surfaces_departure_fields(self):
        """The parsed result must actually carry the delay fields — otherwise
        departure_delay is fed an empty dict forever."""
        from unittest.mock import MagicMock
        raw = {"response": [{
            "dep_iata": "EWR", "arr_iata": "LAX",
            "dep_time": "2026-05-11 18:30", "dep_time_utc": "2026-05-11 22:30",
            "dep_time_ts": time.time() + 3600,
            "arr_time_ts": time.time() + 7200,
            "dep_estimated": "2026-05-11 20:15",
            "dep_estimated_utc": "2026-05-12 00:15",
            "dep_estimated_ts": time.time() + 3600 + 105 * 60,
            "dep_delayed": 105,
            "status": "scheduled", "flight_iata": "UA353",
        }]}
        resp = MagicMock()
        resp.json.return_value = raw
        resp.raise_for_status = MagicMock()

        airlabs._cache.clear()
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get", return_value=resp):
                result = airlabs.get_flight_schedule("UA353")
        airlabs._cache.clear()

        assert result["dep_estimated"] == "2026-05-11 20:15"
        assert result["dep_delayed"] == 105
        assert airlabs.departure_delay(result)[0] == 105


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------

class TestCadence:

    def test_interval_tiers(self):
        ri = tracked_schedule.refresh_interval
        assert ri(30 * 3600) == 3 * 3600      # far out
        assert ri(12 * 3600 + 1) == 3 * 3600
        assert ri(12 * 3600) == 3600          # boundary falls to the mid tier
        assert ri(6 * 3600) == 3600
        assert ri(3 * 3600 + 1) == 3600
        assert ri(3 * 3600) == 900            # boundary falls to the near tier
        assert ri(600) == 900
        assert ri(-4 * 3600) == 900           # departure overdue == delayed

    def test_never_faster_than_the_airlabs_module_cache(self):
        """Inside airlabs.py's own 5-min TTL a 'refresh' is a no-op, so no tier
        may be shorter than it."""
        ri = tracked_schedule.refresh_interval
        for tt in (-10 * 3600, 0, 600, 3 * 3600, 6 * 3600, 30 * 3600, 10 ** 7):
            assert ri(tt) >= airlabs._CACHE_TTL
        assert tracked_schedule._AIRLABS_CACHE_TTL_SEC == airlabs._CACHE_TTL

    @staticmethod
    def _count_dispatches(from_to_dep, to_to_dep, sched=None):
        """Replay the 90s poll loop across a window of time-to-departure on a
        VIRTUAL clock and count how many AirLabs fetches it would trigger.

        Each dispatch is immediately marked complete at the same virtual time,
        which is what the real worker does within a second or two.
        """
        _reset()
        dep_ts = 2_000_000_000.0
        sched = sched or _sched(dep_time_ts=dep_ts, dep_time_utc="")
        start, end = dep_ts - from_to_dep, dep_ts - to_to_dep

        calls = []
        with patch.object(tracked_schedule, "_dispatch",
                          side_effect=lambda cs, pin=None: calls.append(cs)):
            # Seed as if a fetch had just happened at the window's start.
            tracked_schedule.note_fetched("UA353", sched, now=start)
            now = start
            while now <= end:
                before = len(calls)
                tracked_schedule.maybe_refresh("UA353", sched, now=now)
                if len(calls) > before:
                    tracked_schedule.note_fetched("UA353", sched, now=now)
                now += 90.0   # the FR24 poll period
        _reset()
        return len(calls)

    def test_far_tier_polls_every_three_hours(self):
        """30h -> 12h before departure is 18h; at 3h apart that is 6 fetches.
        An hourly cadence here would be 18, a 12-hourly one would be 1."""
        assert self._count_dispatches(30 * 3600, 12 * 3600) == 6

    def test_mid_tier_polls_hourly(self):
        """12h -> 3h is 9h; hourly gives 9."""
        assert self._count_dispatches(12 * 3600, 3 * 3600) == 9

    def test_near_tier_polls_every_fifteen_minutes(self):
        """The final 3h at 15 min apart is 12 fetches."""
        assert self._count_dispatches(3 * 3600, 0) == 12

    def test_tiers_are_strictly_ordered(self):
        """Same-length windows: closer to departure must fetch strictly more."""
        far = self._count_dispatches(21 * 3600, 18 * 3600)   # 3h window, far tier
        mid = self._count_dispatches(9 * 3600, 6 * 3600)     # 3h window, mid tier
        near = self._count_dispatches(3 * 3600, 0)           # 3h window, near tier
        assert far < mid < near

    def test_worst_case_cadence_is_bounded_by_the_36h_tracking_cap(self):
        """Credit ceiling, corrected.

        An earlier version of this test modelled a flight set 7 days ahead. That
        cannot happen: overhead._MAX_TRACKED_HOURS = 36 wipes any tracked flight
        36h after set_ts, so such a flight never reaches the mid/near tiers.

        The real worst case is a flight that goes overdue and is never seen in the
        feed — it sits in the near tier until the cap. Bound that instead.
        """
        near = max(tracked_schedule._INTERVAL_NEAR_SEC, tracked_schedule._AIRLABS_CACHE_TTL_SEC)
        calls_per_hour = 3600 / near
        worst = calls_per_hour * 36            # the hard tracking cap
        assert worst <= 200, (
            f"{worst:.0f} credits worst case per tracked flight — too close to the "
            f"1000/month free tier")
        # and the far tier must actually be cheap
        far = max(tracked_schedule._INTERVAL_FAR_SEC, tracked_schedule._AIRLABS_CACHE_TTL_SEC)
        assert (3600 / far) * 12 <= 12, "far tier is not throttling"

    def test_overdue_departure_uses_the_near_tier(self):
        """Scheduled departure passed and the aircraft still is not in the live
        feed — that IS the delay case, so keep polling at 15 min."""
        assert self._count_dispatches(0, -3 * 3600) == 12

    def test_no_refresh_once_departed(self):
        """An actual departure time means AirLabs has nothing left to revise."""
        departed = _sched(dep_actual="2026-05-11 18:47",
                          dep_actual_ts=_DEP_TS + 17 * 60,
                          status="active")
        assert self._count_dispatches(3 * 3600, -3 * 3600, sched=departed) == 0

    def test_actual_departure_stops_polling_even_when_status_lags(self):
        """AirLabs often still says 'scheduled' after publishing dep_actual, so
        the actual-time check has to stand on its own."""
        departed = _sched(dep_actual="2026-05-11 18:47",
                          dep_actual_ts=_DEP_TS + 17 * 60,
                          status="scheduled")
        assert self._count_dispatches(3 * 3600, -3 * 3600, sched=departed) == 0

    def test_no_refresh_when_cancelled(self):
        cancelled = _sched(status="cancelled")
        assert self._count_dispatches(3 * 3600, 0, sched=cancelled) == 0


# ---------------------------------------------------------------------------
# Non-blocking contract
# ---------------------------------------------------------------------------

class TestEffectiveDeparture:
    """overhead.py flags NOT TRACKABLE 30 min after the departure it expects.
    Gating that on the SCHEDULED time contradicts a delay we are displaying."""

    DEP = _DEP_TS

    def test_no_delay_is_the_scheduled_time(self):
        s = _sched(dep_time_ts=self.DEP)
        assert tracked_schedule.effective_departure_ts(s, None) == self.DEP
        assert tracked_schedule.effective_departure_ts(s, 0) == self.DEP

    def test_delay_pushes_the_expected_departure_out(self):
        s = _sched(dep_time_ts=self.DEP)
        assert tracked_schedule.effective_departure_ts(s, 105) == self.DEP + 105 * 60

    def test_a_delayed_flight_is_not_yet_not_trackable(self):
        """The regression this exists to prevent: 40 min past the scheduled
        departure of a flight running 2h late is still pre-departure."""
        s = _sched(dep_time_ts=self.DEP)
        now = self.DEP + 40 * 60
        assert now > tracked_schedule.effective_departure_ts(s, None) + 1800
        assert now < tracked_schedule.effective_departure_ts(s, 120) + 1800

    def test_falls_back_to_utc_string(self):
        s = _sched(dep_time_ts=None, dep_time_utc="2026-05-11 22:30")
        assert tracked_schedule.effective_departure_ts(s, 30) == self.DEP + 1800

    def test_unparseable_schedule_returns_none(self):
        assert tracked_schedule.effective_departure_ts(
            _sched(dep_time_ts=None, dep_time_utc=""), 30) is None

    def test_garbage_delay_does_not_raise(self):
        s = _sched(dep_time_ts=self.DEP)
        assert tracked_schedule.effective_departure_ts(s, "soon") == self.DEP
        assert tracked_schedule.effective_departure_ts(s, -60) == self.DEP


class TestNonBlocking:

    def test_getter_returns_while_the_fetch_is_still_in_flight(self):
        """A synchronous implementation fails this on wall-clock time.

        The mocked AirLabs call parks on an Event, so any getter that waits for
        it blocks for the full gate timeout instead of the budget below.
        """
        _reset()
        gate = threading.Event()
        started = threading.Event()
        sched = _sched()

        def blocking_fetch(callsign, pin_dep_ts=None):
            started.set()
            gate.wait(10)
            return _sched(dep_delayed=105, dep_estimated="2026-05-11 20:15")

        try:
            with patch("utilities.airlabs.get_flight_schedule",
                       side_effect=blocking_fetch):
                # last fetch at epoch 0 => wildly overdue => refresh dispatched
                tracked_schedule.note_fetched("UA353", sched, now=0)

                t0 = time.monotonic()
                got = tracked_schedule.maybe_refresh("UA353", sched)
                elapsed = time.monotonic() - t0

                assert started.wait(5), "background worker never started"
                assert not gate.is_set(), "fetch already finished — test is not proving anything"
                assert elapsed < 0.25, f"getter blocked for {elapsed:.2f}s"
                assert got is sched, "getter must hand back last-good immediately"

                gate.set()
                settle(tracked_schedule)
        finally:
            gate.set()
            _reset()

    def test_worker_runs_off_the_render_core(self):
        """cpu_affinity.run_off_render_core() must be the worker's first act —
        background CPU on the isolated core starves the LED refresh thread."""
        _reset()
        order = []
        try:
            with patch.object(tracked_schedule, "run_off_render_core",
                              side_effect=lambda: order.append("affinity")):
                with patch("utilities.airlabs.get_flight_schedule",
                           side_effect=lambda cs, pin_dep_ts=None: order.append("fetch") or _sched()):
                    tracked_schedule.note_fetched("UA353", _sched(), now=0)
                    tracked_schedule.maybe_refresh("UA353", _sched())
                    settle(tracked_schedule)
        finally:
            _reset()
        assert order == ["affinity", "fetch"]

    def test_refresh_result_reaches_the_next_getter_call(self):
        _reset()
        sched = _sched()
        fresh = _sched(dep_delayed=105, dep_estimated="2026-05-11 20:15")
        try:
            with patch("utilities.airlabs.get_flight_schedule", return_value=fresh):
                tracked_schedule.note_fetched("UA353", sched, now=0)
                assert tracked_schedule.maybe_refresh("UA353", sched) is sched
                settle(tracked_schedule)
            assert tracked_schedule.maybe_refresh("UA353", sched) is fresh
        finally:
            _reset()

    def test_only_one_fetch_in_flight_at_a_time(self):
        _reset()
        gate = threading.Event()
        calls = []

        def blocking_fetch(callsign, pin_dep_ts=None):
            calls.append(callsign)
            gate.wait(10)
            return _sched()

        try:
            with patch("utilities.airlabs.get_flight_schedule",
                       side_effect=blocking_fetch):
                tracked_schedule.note_fetched("UA353", _sched(), now=0)
                for _ in range(10):
                    tracked_schedule.maybe_refresh("UA353", _sched())
                gate.set()
                settle(tracked_schedule)
            assert len(calls) == 1
        finally:
            gate.set()
            _reset()


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestLastGoodOnError:

    @staticmethod
    def _assert_last_good_intact(good):
        """The cache itself must still hold `good`.

        Re-calling maybe_refresh(cs, good) is NOT sufficient: if the failure
        path dropped the entry, the getter would simply re-seed from the
        argument and hand the same object back, so the assertion would pass
        vacuously. Check the cache, then probe with a DIFFERENT object so a
        re-seed is visible.
        """
        assert tracked_schedule._last_good.get("UA353") is good
        other = _sched(dep_time="1999-01-01 00:00")
        assert tracked_schedule.maybe_refresh("UA353", other) is good

    def test_exception_keeps_last_good(self):
        _reset()
        good = _sched()
        try:
            with patch("utilities.airlabs.get_flight_schedule",
                       side_effect=RuntimeError("AirLabs exploded")):
                tracked_schedule.note_fetched("UA353", good, now=0)
                assert tracked_schedule.maybe_refresh("UA353", good) is good
                settle(tracked_schedule)
            self._assert_last_good_intact(good)
        finally:
            _reset()

    def test_empty_response_keeps_last_good(self):
        """AirLabs returning None must not blank a departure time that is still
        the best thing we know."""
        _reset()
        good = _sched()
        try:
            with patch("utilities.airlabs.get_flight_schedule", return_value=None):
                tracked_schedule.note_fetched("UA353", good, now=0)
                tracked_schedule.maybe_refresh("UA353", good)
                settle(tracked_schedule)
            self._assert_last_good_intact(good)
        finally:
            _reset()

    def test_failed_attempt_is_stamped_so_it_does_not_retry_every_poll(self):
        """Otherwise a bounded cadence becomes unbounded exactly when the API
        is unhealthy."""
        _reset()
        calls = []
        try:
            with patch("utilities.airlabs.get_flight_schedule",
                       side_effect=lambda cs, pin_dep_ts=None: calls.append(cs) or (_ for _ in ()).throw(
                           RuntimeError("down"))):
                tracked_schedule.note_fetched("UA353", _sched(), now=0)
                tracked_schedule.maybe_refresh("UA353", _sched())
                settle(tracked_schedule)
                # Immediately afterwards the cadence must hold us off.
                for _ in range(5):
                    tracked_schedule.maybe_refresh("UA353", _sched())
                settle(tracked_schedule)
            assert len(calls) == 1
        finally:
            _reset()

    def test_thread_start_failure_does_not_wedge_the_pending_flag(self):
        """Thread.start() can fail under memory pressure on the 512MB Pi."""
        _reset()
        try:
            with patch.object(tracked_schedule.threading, "Thread",
                              side_effect=RuntimeError("can't start new thread")):
                tracked_schedule.note_fetched("UA353", _sched(), now=0)
                tracked_schedule.maybe_refresh("UA353", _sched())
            assert tracked_schedule._refresh_pending is False
        finally:
            _reset()

    def test_forget_clears_state(self):
        """overhead.py clears _tracked_schedule_cache on a flight change; this
        cache must not keep the old flight's fetch timestamp."""
        _reset()
        tracked_schedule.note_fetched("UA353", _sched(), now=123.0)
        assert tracked_schedule._last_good.get("UA353") is not None
        tracked_schedule.forget()
        assert tracked_schedule._last_good == {}
        assert tracked_schedule._last_fetch_ts == {}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _text(parts):
    return "".join(ch for ch, _ in parts)


def _colour_of(parts, substring):
    """Colour of the first character of `substring` in the rendered parts."""
    rendered = _text(parts)
    idx = rendered.index(substring)
    return parts[idx][1]


class TestRendering:
    BASE = {
        "is_scheduled": True,
        "dep_time": "2026-05-11 18:30",
        "origin": "EWR",
        "destination": "LAX",
    }

    def test_delay_changes_the_rendered_line(self):
        from scenes.trackedstats import _build_stats

        on_time = _text(_build_stats(dict(self.BASE)))
        delayed = _text(_build_stats(dict(
            self.BASE, dep_delay_min=105, dep_time_revised="2026-05-11 20:15")))

        assert delayed != on_time
        # Scheduled time shown when on time; revised time when delayed
        # (accept either clock format so the test does not pin CLOCK_FORMAT).
        assert "18:30" in on_time or "6:30p" in on_time
        assert "20:15" in delayed or "8:15p" in delayed
        assert "18:30" not in delayed and "6:30p" not in delayed
        assert "(+1:45)" in delayed
        assert "(+" not in on_time
        # Route survives either way
        assert "EWR→LAX" in on_time and "EWR→LAX" in delayed

    def test_short_delay_uses_minutes_format(self):
        from scenes.trackedstats import _build_stats
        line = _text(_build_stats(dict(
            self.BASE, dep_delay_min=40, dep_time_revised="2026-05-11 19:10")))
        assert "(+40m)" in line

    def test_delay_colour_bands(self):
        from scenes.trackedstats import _build_stats, TIME_DIST_COLOUR
        from setup import colours

        cases = [
            (14, TIME_DIST_COLOUR),          # below threshold: unchanged
            (15, colours.LIGHT_YELLOW),
            (44, colours.LIGHT_YELLOW),
            (45, colours.LIGHT_ORANGE),
            (89, colours.LIGHT_ORANGE),
            (90, colours.LIGHT_RED),
            (600, colours.LIGHT_RED),
        ]
        for mins, expected in cases:
            parts = _build_stats(dict(
                self.BASE, dep_delay_min=mins,
                dep_time_revised="2026-05-11 20:15"))
            # "Departs " is always the base colour; the TIME carries the band.
            assert _colour_of(parts, "Departs") == TIME_DIST_COLOUR
            time_colour = parts[len("Departs ")][1]
            assert time_colour == expected, f"{mins} min -> wrong colour"

    def test_sub_threshold_delay_shows_the_scheduled_time(self):
        """A 10-minute slip is not worth the panel width."""
        from scenes.trackedstats import _build_stats
        line = _text(_build_stats(dict(
            self.BASE, dep_delay_min=10, dep_time_revised="2026-05-11 18:40")))
        assert "(+" not in line
        assert "18:30" in line or "6:30p" in line

    def test_unknown_delay_renders_exactly_as_before(self):
        """dep_delay_min None (AirLabs said nothing) must be byte-identical to
        the pre-feature output."""
        from scenes.trackedstats import _build_stats
        before = _build_stats(dict(self.BASE))
        with_nulls = _build_stats(dict(
            self.BASE, dep_delay_min=None, dep_time_revised=""))
        assert _text(before) == _text(with_nulls)
        assert _text(before) == "Departs 18:30 EWR→LAX" or "Departs" in _text(before)

    def test_known_on_time_is_not_treated_as_delayed(self):
        from scenes.trackedstats import _build_stats
        line = _text(_build_stats(dict(self.BASE, dep_delay_min=0)))
        assert "(+" not in line
        assert "18:30" in line or "6:30p" in line

    def test_delay_without_a_revised_time_keeps_the_scheduled_time(self):
        from scenes.trackedstats import _build_stats
        line = _text(_build_stats(dict(
            self.BASE, dep_delay_min=105, dep_time_revised="")))
        assert "18:30" in line or "6:30p" in line

    def test_missing_dep_time_still_falls_back_to_scheduled_label(self):
        from scenes.trackedstats import _build_stats
        line = _text(_build_stats(
            {"is_scheduled": True, "origin": "EWR", "destination": "LAX"}))
        assert line == "Scheduled EWR→LAX"
