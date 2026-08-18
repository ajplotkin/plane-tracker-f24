"""test_leg_pinning.py — a completed tracked flight actually clears.

THE BUG. A tracked flight that never appeared in the live feed could not
complete. utilities/airlabs.get_flight_schedule kept legs arriving after
`now - 7200` and picked the soonest-departing one; utilities/overhead wiped at
`now > arrival + 7200`. Exact negations: while the dead leg was returned the
wipe could not fire, and the moment it aged out the NEXT DAY's leg of the same
daily flight number was substituted, arrival a day in the future, forever. Only
the 36h _MAX_TRACKED_HOURS cap eventually stopped it — and even that sat behind
the FR24 zone fetch, whose network handler preserves the last tracked data, so
a sustained FR24 outage outlived the backstop too.

WHAT THESE TESTS PIN DOWN, in the order the data flows:

  * airlabs picks the PINNED leg — nearest scheduled departure — and reports
    LEG_GONE (a valid response without our leg) DISTINCTLY from None (the API
    failed). Those two must never be confused: one may wipe, the other may not.
  * the four completion rules, as a pure function.
  * the WIRING: the pin actually reaches all three fetch sites in overhead's
    never-live branch. The delay feature shipped with untested wiring once and
    a mutation that disabled it left the whole suite green, so these drive
    Overhead._grab() and assert on what the fetchers were CALLED WITH, not on
    helpers in isolation.
  * the original zombie, end to end, against a two-leg AirLabs payload.
"""

import json
import os
import sys
import tempfile
import time as _time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("ZONE_TL_LAT", "51.7")
os.environ.setdefault("ZONE_TL_LON", "-0.3")
os.environ.setdefault("ZONE_BR_LAT", "51.47")
os.environ.setdefault("ZONE_BR_LON", "-0.111")
os.environ.setdefault("HOME_LAT", "51.55864")
os.environ.setdefault("HOME_LON", "-0.177332")
os.environ.setdefault("DISTANCE_UNITS", "imperial")
os.environ.setdefault("PLANE_TRACKER_DATA_DIR", tempfile.mkdtemp())

import utilities.airlabs as airlabs                     # noqa: E402
import utilities.overhead as overhead                   # noqa: E402
import utilities.tracked_schedule as tracked_schedule   # noqa: E402
from tests.conftest import settle                       # noqa: E402


HOUR = 3600


def _leg(dep_ts, arr_ts, **over):
    """One raw AirLabs /schedules row."""
    s = {
        "dep_iata": "EWR", "arr_iata": "LAX",
        "dep_time": "2026-05-11 18:30", "dep_time_utc": "2026-05-11 22:30",
        "dep_time_ts": dep_ts,
        "arr_time": "2026-05-11 21:45",
        "arr_time_utc": _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(arr_ts)),
        "arr_time_ts": arr_ts,
        "status": "scheduled",
        "airline_iata": "UA", "flight_iata": "UA353",
        "duration": 330,
    }
    s.update(over)
    return s


def _response(*legs):
    r = MagicMock()
    r.json.return_value = {"response": list(legs)}
    r.raise_for_status = MagicMock()
    return r


def _reset_caches():
    airlabs._cache.clear()
    tracked_schedule.forget()


# ═══════════════════════════════════════════════════════════════════════════
# 1. airlabs: pinned leg selection
# ═══════════════════════════════════════════════════════════════════════════

class TestPinnedSelection:

    def setup_method(self):
        _reset_caches()

    def _fetch(self, legs, pin):
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=_response(*legs)):
                return airlabs.get_flight_schedule("UA353", pin_dep_ts=pin)

    def test_picks_the_pinned_leg_among_same_day_legs(self):
        """A flight number flying three legs in a day: 2-3h apart, all inside
        any plausible window, so only NEAREST-match resolves the right one."""
        now = _time.time()
        legs = [
            _leg(now - 6 * HOUR, now - 3 * HOUR, dep_iata="SFO", arr_iata="EWR"),
            _leg(now - 3 * HOUR, now + 1 * HOUR, dep_iata="EWR", arr_iata="ORD"),
            _leg(now + 1 * HOUR, now + 5 * HOUR, dep_iata="ORD", arr_iata="LAX"),
        ]
        assert self._fetch(legs, now - 6 * HOUR)["destination"] == "EWR"
        _reset_caches()
        assert self._fetch(legs, now - 3 * HOUR)["destination"] == "ORD"
        _reset_caches()
        assert self._fetch(legs, now + 1 * HOUR)["destination"] == "LAX"

    def test_a_delayed_leg_still_matches_its_pin(self):
        """The pin is the SCHEDULED departure, which a delay does not move —
        matching on dep_estimated instead would lose the leg mid-delay."""
        now = _time.time()
        leg = _leg(now + HOUR, now + 6 * HOUR,
                   dep_estimated_ts=now + 4 * HOUR, dep_delayed=180)
        got = self._fetch([leg], now + HOUR)
        assert got and got["dep_delayed"] == 180

    def test_the_next_days_leg_is_never_substituted(self):
        """THE REGRESSION. Today's leg has finished and dropped out of the
        response; tomorrow's leg of the same daily flight number is all that is
        left. Returning it is what made a dead flight immortal."""
        now = _time.time()
        tomorrow = _leg(now + 20 * HOUR, now + 25 * HOUR)
        got = self._fetch([tomorrow], now - 4 * HOUR)
        assert got is airlabs.LEG_GONE
        assert got != tomorrow

    def test_a_leg_just_outside_the_tolerance_is_gone(self):
        now = _time.time()
        inside = self._fetch([_leg(now + airlabs._PIN_TOLERANCE_SEC - 60,
                                   now + 5 * HOUR)], now)
        assert inside is not airlabs.LEG_GONE and inside
        _reset_caches()
        outside = self._fetch([_leg(now + airlabs._PIN_TOLERANCE_SEC + 60,
                                    now + 5 * HOUR)], now)
        assert outside is airlabs.LEG_GONE

    def test_empty_response_with_a_pin_is_leg_gone_not_none(self):
        """A finished flight simply stops being scheduled. That is a VALID
        answer meaning 'gone', not an API failure."""
        assert self._fetch([], _time.time()) is airlabs.LEG_GONE

    def test_api_failure_is_none_and_never_leg_gone(self):
        """Rule 4 lives or dies here: a timeout must not read as completion."""
        import requests as req
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       side_effect=req.exceptions.Timeout):
                got = airlabs.get_flight_schedule("UA353", pin_dep_ts=_time.time())
        assert got is None
        assert got is not airlabs.LEG_GONE

    def test_leg_gone_is_falsy_so_naive_callers_treat_it_as_no_data(self):
        assert not airlabs.LEG_GONE
        assert bool(airlabs.LEG_GONE) is False
        assert airlabs.LEG_GONE is not None

    def test_legs_without_a_departure_timestamp_cannot_match(self):
        now = _time.time()
        got = self._fetch([_leg(None, now + 5 * HOUR)], now)
        assert got is airlabs.LEG_GONE

    def test_an_unusable_pin_degrades_to_unpinned_selection(self):
        """Garbage in tracked_flight.json must not read as 'the leg is gone'."""
        now = _time.time()
        got = self._fetch([_leg(now + HOUR, now + 6 * HOUR)], "not-a-timestamp")
        assert got is not airlabs.LEG_GONE
        assert got["origin"] == "EWR"

    def test_pin_is_part_of_the_cache_key(self):
        """Two pins are two questions; sharing one cache slot answers the
        second with the first leg."""
        now = _time.time()
        legs = [_leg(now - 3 * HOUR, now + HOUR, arr_iata="ORD"),
                _leg(now + HOUR, now + 5 * HOUR, arr_iata="LAX")]
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=_response(*legs)):
                first = airlabs.get_flight_schedule("UA353", pin_dep_ts=now - 3 * HOUR)
                second = airlabs.get_flight_schedule("UA353", pin_dep_ts=now + HOUR)
                unpinned = airlabs.get_flight_schedule("UA353")
        assert first["destination"] == "ORD"
        assert second["destination"] == "LAX"
        assert unpinned["destination"] == "ORD"     # soonest-departing, as before

    def test_the_pinned_result_is_cached(self):
        """Credit burn must go DOWN, not up: repeat polls hit the module cache."""
        now = _time.time()
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=_response(_leg(now, now + 5 * HOUR))) as g:
                for _ in range(5):
                    airlabs.get_flight_schedule("UA353", pin_dep_ts=now)
        assert g.call_count == 1

    def test_leg_gone_is_cached_too(self):
        now = _time.time()
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=_response()) as g:
                for _ in range(5):
                    assert airlabs.get_flight_schedule(
                        "UA353", pin_dep_ts=now) is airlabs.LEG_GONE
        assert g.call_count == 1

    def test_unpinned_behaviour_is_unchanged(self):
        """The web UI's lookup path passes no pin and must be untouched."""
        now = _time.time()
        legs = [_leg(now + 5 * HOUR, now + 9 * HOUR, arr_iata="ORD"),
                _leg(now + HOUR, now + 6 * HOUR, arr_iata="LAX")]
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=_response(*legs)):
                got = airlabs.get_flight_schedule("UA353")
        assert got["destination"] == "LAX"     # soonest departing

    def test_the_two_tolerances_agree(self):
        """airlabs decides which leg IS the pinned one; overhead re-checks the
        same thing in its was-live reality check. Two constants, one meaning —
        if they drift, a leg is 'mine' to one module and not the other."""
        assert airlabs._PIN_TOLERANCE_SEC == overhead._PIN_TOLERANCE_SEC

    def test_get_flight_legs_cannot_clobber_a_pinned_entry(self):
        """The multi-leg picker writes the SOONEST leg into the cache. Sharing
        a key with a pinned lookup would hand the tracker the wrong leg."""
        now = _time.time()
        legs = [_leg(now - 3 * HOUR, now + HOUR, arr_iata="ORD"),
                _leg(now + HOUR, now + 5 * HOUR, arr_iata="LAX")]
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=_response(*legs)) as http:
                pinned = airlabs.get_flight_schedule("UA353", pin_dep_ts=now + HOUR)
                airlabs.get_flight_legs("UA353")
                calls_before = http.call_count
                again = airlabs.get_flight_schedule("UA353", pin_dep_ts=now + HOUR)
                calls_after = http.call_count
        assert pinned["destination"] == "LAX"
        assert again["destination"] == "LAX"
        assert calls_after == calls_before, (
            "the pinned cache entry did not survive get_flight_legs — every "
            "multi-leg lookup would then cost the tracker another credit")

    def test_get_pinned_schedule_wrapper_matches_the_keyword_form(self):
        now = _time.time()
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=_response(_leg(now + 20 * HOUR, now + 25 * HOUR))):
                assert airlabs.get_pinned_schedule("UA353", now) is airlabs.LEG_GONE


# ═══════════════════════════════════════════════════════════════════════════
# 2. Completion rules
# ═══════════════════════════════════════════════════════════════════════════

def _sched(dep_ts, arr_ts, **over):
    """A schedule as get_flight_schedule returns it."""
    s = {
        "origin": "EWR", "destination": "LAX",
        "dep_time": "2026-05-11 18:30", "dep_time_utc": "2026-05-11 22:30",
        "dep_time_ts": dep_ts,
        "arr_time_utc": _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(arr_ts)),
        "arr_time_ts": arr_ts,
        "arr_estimated_utc": "", "arr_actual_utc": "",
        "status": "scheduled", "duration": 330,
    }
    s.update(over)
    return s


def _utc(ts):
    return _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(ts))


class TestBestArrival:

    def test_actual_beats_estimated_beats_scheduled(self):
        now = _time.time()
        s = _sched(now - 6 * HOUR, now - HOUR,
                   arr_estimated_utc=_utc(now - 30 * 60),
                   arr_actual_utc=_utc(now - 20 * 60))
        assert overhead.best_arrival_ts(s) == pytest.approx(now - 20 * 60, abs=60)
        s.pop("arr_actual_utc")
        assert overhead.best_arrival_ts(s) == pytest.approx(now - 30 * 60, abs=60)
        s.pop("arr_estimated_utc")
        assert overhead.best_arrival_ts(s) == pytest.approx(now - HOUR, abs=60)

    def test_falls_back_to_departure_plus_block_time(self):
        """The free AirLabs tier does not promise the arrival fields."""
        now = _time.time()
        s = _sched(now, now + 5 * HOUR)
        s["arr_time_utc"] = ""
        s["arr_time_ts"] = None
        assert overhead.best_arrival_ts(s) == pytest.approx(now + 330 * 60, abs=1)

    def test_nothing_usable_is_none(self):
        assert overhead.best_arrival_ts(None) is None
        assert overhead.best_arrival_ts({}) is None
        assert overhead.best_arrival_ts({"arr_time_utc": "garbage"}) is None


class TestLegMatchesPin:

    def test_matches_within_tolerance(self):
        now = _time.time()
        assert overhead.leg_matches_pin(_sched(now + 600, now + 5 * HOUR), now)

    def test_the_next_days_leg_does_not_match(self):
        now = _time.time()
        assert not overhead.leg_matches_pin(
            _sched(now + 24 * HOUR, now + 29 * HOUR), now)

    def test_no_pin_accepts_any_schedule_but_not_none(self):
        now = _time.time()
        assert overhead.leg_matches_pin(_sched(now, now + HOUR), None)
        assert not overhead.leg_matches_pin(None, None)


class TestCompletionRules:
    """The four rules. `now` is fixed; every case differs only in the data."""

    NOW = 1_800_000_000.0
    PIN = NOW - 8 * HOUR

    def _decide(self, sched=None, leg_gone=False, cached_route=None, pin=None):
        return overhead.tracked_completion_decision(
            sched, leg_gone, self.PIN if pin is None else pin,
            cached_route, self.NOW)

    # --- Rule 1: landed ---------------------------------------------------

    def test_rule1_landed_pinned_leg_wipes(self):
        wipe, why = self._decide(_sched(self.PIN, self.NOW - 30 * 60,
                                        status="landed"))
        assert wipe and "landed" in why

    def test_rule1_wipes_even_though_the_arrival_grace_has_not_run_out(self):
        """This is rule 1's whole purpose: 'landed' 30 minutes ago is over, and
        waiting another 90 minutes to say so is just a stale panel."""
        s = _sched(self.PIN, self.NOW - 30 * 60, status="landed")
        assert self._decide(s)[0] is True
        assert self._decide(dict(s, status="scheduled"))[0] is False

    def test_rule1_does_not_wipe_a_leg_that_has_not_departed(self):
        """A 'landed' status on a leg still hours from pushback is stale data
        about some other day; the departure guard is what catches it."""
        future = self.NOW + 4 * HOUR
        wipe, _ = self._decide(_sched(future, future + 5 * HOUR, status="landed"),
                               pin=future)
        assert wipe is False

    # --- Rule 2: arrival + 2h --------------------------------------------

    def test_rule2_arrival_more_than_two_hours_ago_wipes(self):
        wipe, why = self._decide(_sched(self.PIN, self.NOW - 2 * HOUR - 60))
        assert wipe and "arrival" in why

    def test_rule2_does_not_fire_inside_the_grace(self):
        assert self._decide(_sched(self.PIN, self.NOW - 2 * HOUR + 60))[0] is False

    def test_rule2_uses_the_actual_arrival_when_there_is_one(self):
        """Landed 3h early: the SCHEDULED arrival is still in the future, but
        the flight is plainly over."""
        s = _sched(self.PIN, self.NOW + HOUR,
                   arr_actual_utc=_utc(self.NOW - 3 * HOUR))
        assert self._decide(s)[0] is True

    def test_rule2_respects_a_pushed_out_estimate(self):
        """Running 4h late: scheduled arrival long past, estimate still ahead.
        Wiping on the scheduled time would erase a flight still in the air."""
        s = _sched(self.PIN, self.NOW - 5 * HOUR,
                   arr_estimated_utc=_utc(self.NOW + HOUR))
        assert self._decide(s)[0] is False

    # --- Rule 3: leg gone -------------------------------------------------

    def test_rule3_gone_and_arrival_passed_wipes(self):
        route = {"time_scheduled_arrival": self.NOW - 3 * HOUR}
        wipe, why = self._decide(leg_gone=True, cached_route=route)
        assert wipe and "gone" in why

    def test_rule3_gone_but_arrival_not_yet_passed_keeps(self):
        """A retimed leg drops out of the response before its new time; the 36h
        cap is the backstop, not a premature wipe."""
        route = {"time_scheduled_arrival": self.NOW + HOUR}
        wipe, _ = self._decide(leg_gone=True, cached_route=route)
        assert wipe is False

    def test_rule3_falls_back_to_the_pin_plus_block_time(self):
        """No cached route: the pin plus the last known duration is the only
        arrival we have."""
        stale = _sched(self.PIN, self.NOW)
        stale["arr_time_utc"], stale["arr_time_ts"] = "", None
        wipe, _ = self._decide(leg_gone=True, sched=stale)   # PIN + 5.5h < NOW - 2h
        assert wipe is True

    def test_rule3_with_nothing_to_go_on_keeps(self):
        assert self._decide(leg_gone=True, pin=None)[0] is False

    def test_rule3_takes_the_latest_arrival_of_all_its_sources(self):
        """Each source alone would wipe; together the latest one must win."""
        route = {"time_scheduled_arrival": self.NOW - 5 * HOUR}
        delayed = _sched(self.PIN, self.NOW - 4 * HOUR,
                         arr_estimated_utc=_utc(self.NOW - 30 * 60))
        assert overhead.saved_arrival_ts(route, self.PIN, delayed) == pytest.approx(
            self.NOW - 30 * 60, abs=60)
        assert self._decide(delayed, leg_gone=True, cached_route=route)[0] is False

    def test_rule3_outranks_a_cached_schedule_that_says_otherwise(self):
        """Gone is fresher news than the schedule we hold, so rule 3 decides —
        but on the LATEST arrival either source knows, because the disagreement
        means the leg was retimed and may still be flying."""
        route = {"time_scheduled_arrival": self.NOW - 3 * HOUR}
        delayed = _sched(self.PIN, self.NOW - 3 * HOUR,
                         arr_estimated_utc=_utc(self.NOW + HOUR))
        assert self._decide(delayed, leg_gone=True, cached_route=route)[0] is False
        # Both arrivals past the grace: nothing left to wait for.
        done = _sched(self.PIN, self.NOW - 4 * HOUR)
        assert self._decide(done, leg_gone=True, cached_route=route)[0] is True

    def test_rule3_uses_the_last_schedule_when_there_is_no_cached_route(self):
        """A flight tracked blind has no cached route; the leg we last held is
        then the only arrival there is."""
        recent = _sched(self.PIN, self.NOW - HOUR)     # inside the grace
        assert self._decide(recent, leg_gone=True)[0] is False
        old = _sched(self.PIN, self.NOW - 3 * HOUR)
        assert self._decide(old, leg_gone=True)[0] is True

    # --- Rule 4: API error ------------------------------------------------

    def test_rule4_api_failure_never_wipes(self):
        wipe, why = self._decide(None, leg_gone=False)
        assert wipe is False and "API failure" in why

    def test_rule4_holds_even_long_past_the_arrival(self):
        """No schedule and no verdict means we know nothing new — including
        after the flight should have landed. Only the 36h cap ends this."""
        assert overhead.tracked_completion_decision(
            None, False, self.PIN, {"time_scheduled_arrival": self.NOW - 20 * HOUR},
            self.NOW)[0] is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. Wiring — the pin reaches all three fetch sites in Overhead._grab
# ═══════════════════════════════════════════════════════════════════════════

PIN = None      # set per-test


class _Rig:
    """An Overhead driven through one _grab() with no live feed and no network."""

    def __init__(self, tmp_path, monkeypatch, tracked):
        self.tmp = tmp_path
        self.file = os.path.join(str(tmp_path), "tracked_flight.json")
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(tracked, f)
        monkeypatch.setattr(overhead, "TRACKED_FILE", self.file)
        monkeypatch.setattr(overhead, "DATA_DIR", str(tmp_path))
        self.o = overhead.Overhead()
        self.o._api = MagicMock()
        self.o._api.get_flights.return_value = []
        self.o._api.fr24_ok = True
        # Not in the live feed — the never-live branch is what we are testing.
        self.o._grab_tracked = MagicMock(return_value=None)
        self.fetches = []           # (callsign, pin) per airlabs lookup
        self.refreshes = []         # (callsign, pin) per maybe_refresh
        _reset_caches()

    def airlabs_returns(self, *results):
        """Queue per-call results; the last one repeats."""
        queue = list(results)

        def fake(callsign, pin_dep_ts=None):
            self.fetches.append((callsign, pin_dep_ts))
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return patch("utilities.airlabs.get_flight_schedule", side_effect=fake)

    def record_refresh(self):
        def fake(callsign, sched, now=None, pin_dep_ts=None):
            self.refreshes.append((callsign, pin_dep_ts))
            return sched
        return patch.object(tracked_schedule, "maybe_refresh", side_effect=fake)

    def warm(self, sched, callsign="UA353"):
        """Pre-seed the schedule cache as a poll two-or-later would find it.

        _tracked_last_callsign has to match, or _grab treats this as a brand
        new tracked flight and clears the cache before reading it.
        """
        self.o._tracked_last_callsign = callsign
        self.o._tracked_schedule_cache[callsign] = sched

    def tracked_file(self):
        with open(self.file, encoding="utf-8") as f:
            return json.load(f)

    def grab(self):
        with patch("utilities.iss.get_iss_pass_data", return_value=None):
            self.o._grab()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    def _make(tracked):
        return _Rig(tmp_path, monkeypatch, tracked)
    yield _make
    _reset_caches()


def _tracked(pin=None, set_ts=None, route=None, callsign="UA353"):
    now = _time.time()
    d = {"callsign": callsign, "set_ts": int(set_ts if set_ts is not None else now)}
    if pin is not None:
        d["scheduled_departure"] = pin
    if route is not None:
        d["cached_route"] = route
    return d


class TestPinIsThreadedThroughOverhead:
    """A previous feature shipped with its wiring untested and a mutation that
    disabled it left the suite green. These assert what the fetchers were
    called WITH, from a real _grab()."""

    def test_site_one_cold_fetch(self, rig):
        now = _time.time()
        pin = now + 2 * HOUR
        r = rig(_tracked(pin=pin))
        with r.airlabs_returns(_sched(pin, pin + 5 * HOUR)):
            r.grab()
        assert r.fetches, "the cold fetch never happened"
        assert r.fetches[0] == ("UA353", pin), (
            f"cold fetch was not pinned: {r.fetches[0]}")

    def test_site_two_maybe_refresh(self, rig):
        now = _time.time()
        pin = now + 2 * HOUR
        r = rig(_tracked(pin=pin))
        # Warm cache with a leg whose arrival is still ahead, so the
        # arrival-expiry re-fetch cannot fire and only site 2 can run.
        r.warm(_sched(pin, pin + 5 * HOUR))
        with r.record_refresh():
            with r.airlabs_returns(None):
                r.grab()
        assert r.refreshes == [("UA353", pin)], (
            f"maybe_refresh was not pinned: {r.refreshes}")
        assert r.fetches == [], "no direct fetch should happen on this path"

    def test_site_three_arrival_expiry_refetch(self, rig):
        now = _time.time()
        pin = now - 8 * HOUR
        r = rig(_tracked(pin=pin))
        # Cached leg arrived 3h ago -> past the +1h re-fetch trigger. Site 2
        # runs first and hands the cached leg straight back (no I/O), so every
        # lookup recorded here is the expiry re-fetch.
        r.warm(_sched(pin, now - 3 * HOUR))
        with r.airlabs_returns(_sched(pin, now - 3 * HOUR)):
            r.grab()
        assert len(r.fetches) == 1, (
            f"expected exactly the expiry re-fetch, got {r.fetches}")
        assert all(call == ("UA353", pin) for call in r.fetches), (
            f"the expiry re-fetch was not pinned: {r.fetches}")

    def test_the_refresher_passes_the_pin_on_to_airlabs(self):
        """maybe_refresh -> _dispatch -> _background_refresh -> airlabs."""
        _reset_caches()
        pin = _time.time() + 3 * HOUR
        seen = []

        def fake(callsign, pin_dep_ts=None):
            seen.append((callsign, pin_dep_ts))
            return _sched(pin, pin + 5 * HOUR)
        try:
            with patch("utilities.airlabs.get_flight_schedule", side_effect=fake):
                sched = _sched(pin, pin + 5 * HOUR)
                tracked_schedule.note_fetched("UA353", sched, now=0)
                tracked_schedule.maybe_refresh("UA353", sched, pin_dep_ts=pin)
                settle(tracked_schedule)
        finally:
            _reset_caches()
        assert seen == [("UA353", pin)], f"pin lost in the refresher: {seen}"


class TestNeverLiveCompletion:
    """End-to-end through _grab(): does the flight actually clear?"""

    def test_the_zombie_clears(self, rig):
        """THE ORIGINAL BUG, against a real two-leg AirLabs payload: today's
        finished leg plus tomorrow's. Unpinned, today's leg is filtered out
        (arrived >2h ago), tomorrow's is substituted, its arrival is a day away
        and the wipe can never fire."""
        now = _time.time()
        pin = now - 8 * HOUR
        r = rig(_tracked(pin=pin,
                         route={"time_scheduled_arrival": now - 3 * HOUR}))
        payload = _response(
            _leg(pin, now - 3 * HOUR, status="landed"),
            _leg(now + 16 * HOUR, now + 21 * HOUR),
        )
        with patch.object(airlabs, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get",
                       return_value=payload) as http:
                r.grab()
        assert r.tracked_file()["callsign"] == "", "the dead flight was not wiped"
        assert http.call_count == 1, (
            f"leg pinning must not cost extra credits ({http.call_count} calls)")

    def test_an_api_failure_keeps_the_flight(self, rig):
        """Rule 4 end-to-end: the expiry re-fetch fails, and the cached leg's
        arrival has NOT passed the grace — nothing has told us the flight is
        over, so it stays. (A failed re-fetch reads as LEG_GONE only if the
        two are confused, which would wipe a flight on every AirLabs outage.)"""
        now = _time.time()
        pin = now - 6 * HOUR
        r = rig(_tracked(pin=pin,
                         route={"time_scheduled_arrival": now - 90 * 60}))
        r.warm(_sched(pin, now - 90 * 60))     # past the +1h re-fetch trigger
        with r.airlabs_returns(None):
            r.grab()
        assert r.fetches, "the expiry re-fetch did not run — test proves nothing"
        assert r.tracked_file()["callsign"] == "UA353"

    def test_a_cold_fetch_failure_keeps_the_flight(self, rig):
        """No schedule at all and no leg-gone verdict: keep, whatever the clock
        says. Only the 36h cap ends this one."""
        now = _time.time()
        r = rig(_tracked(pin=now - 20 * HOUR,
                         route={"time_scheduled_arrival": now - 15 * HOUR}))
        with r.airlabs_returns(None):
            r.grab()
        assert r.tracked_file()["callsign"] == "UA353"

    def test_an_api_failure_does_not_rescue_a_long_finished_leg(self, rig):
        """The complement: the cached PINNED leg landed 3h ago. A failed
        re-fetch is no reason to keep showing it — the pin means that arrival
        is still about the right flight (this is the pre-existing behaviour)."""
        now = _time.time()
        pin = now - 8 * HOUR
        r = rig(_tracked(pin=pin))
        r.warm(_sched(pin, now - 3 * HOUR))
        with r.airlabs_returns(None):
            r.grab()
        assert r.tracked_file()["callsign"] == ""

    def test_a_gone_leg_whose_arrival_has_passed_clears(self, rig):
        now = _time.time()
        pin = now - 8 * HOUR
        r = rig(_tracked(pin=pin,
                         route={"time_scheduled_arrival": now - 3 * HOUR}))
        with r.airlabs_returns(airlabs.LEG_GONE):
            r.grab()
        assert r.tracked_file()["callsign"] == ""

    def test_a_gone_leg_is_re_evaluated_on_every_later_poll(self, rig):
        """The gone VERDICT must not be parked in the schedule cache: a cached
        LEG_GONE is falsy but not None, so the next poll would skip the cold
        fetch, find no schedule and no verdict, and keep the flight forever."""
        now = _time.time()
        pin = now - 8 * HOUR
        r = rig(_tracked(pin=pin,
                         route={"time_scheduled_arrival": now + HOUR}))
        with r.airlabs_returns(airlabs.LEG_GONE):
            r.grab()
            assert r.tracked_file()["callsign"] == "UA353"   # not due yet
            # Same flight, but now its saved arrival is well past.
            with open(r.file, "w", encoding="utf-8") as f:
                json.dump(_tracked(pin=pin,
                                   route={"time_scheduled_arrival": now - 3 * HOUR}), f)
            r.grab()
        assert r.tracked_file()["callsign"] == "", (
            "the second poll never re-asked — a stale LEG_GONE is cached")

    def test_the_expiry_refetch_acts_on_a_gone_verdict(self, rig):
        """If the re-fetch's LEG_GONE is treated as 'nothing fresh', the cached
        leg decides instead — and here that wipes a flight whose latest known
        arrival (the route cached when it was chosen) has not passed yet."""
        now = _time.time()
        pin = now - 6 * HOUR
        r = rig(_tracked(pin=pin, route={"time_scheduled_arrival": now}))
        r.warm(_sched(pin, now - 3 * HOUR))       # older than the cached route
        with r.airlabs_returns(airlabs.LEG_GONE):
            r.grab()
        assert r.fetches, "the expiry re-fetch did not run — test proves nothing"
        assert r.tracked_file()["callsign"] == "UA353", (
            "a LEG_GONE from the expiry re-fetch was not routed through rule 3")

    def test_a_gone_leg_whose_arrival_is_still_ahead_is_kept(self, rig):
        now = _time.time()
        pin = now + 2 * HOUR
        r = rig(_tracked(pin=pin,
                         route={"time_scheduled_arrival": now + 7 * HOUR}))
        with r.airlabs_returns(airlabs.LEG_GONE):
            r.grab()
        assert r.tracked_file()["callsign"] == "UA353"

    def test_a_pre_departure_flight_is_untouched_and_still_displayed(self, rig):
        now = _time.time()
        pin = now + 20 * 60      # inside the 30-min FR24 polling window
        r = rig(_tracked(pin=pin))
        with r.airlabs_returns(_sched(pin, now + 5 * HOUR)):
            r.grab()
        assert r.tracked_file()["callsign"] == "UA353"
        assert r.o._tracked_data is not None
        assert r.o._tracked_data["is_scheduled"] is True

    def test_the_display_stands_one_more_cycle_after_the_wipe(self, rig):
        """The panel should not blank mid-scroll: the poll that decides the
        flight is over still renders it, and the NEXT poll finds nothing."""
        now = _time.time()
        pin = now - 8 * HOUR
        r = rig(_tracked(pin=pin))
        with r.airlabs_returns(_sched(pin, now - 3 * HOUR, status="landed")):
            r.grab()
            assert r.o._tracked_data is not None
            assert r.tracked_file()["callsign"] == ""
            r.grab()
        assert r.o._tracked_data is None


class TestSelfPin:
    """(D) A flight tracked blind adopts the leg it resolves to."""

    def test_a_blind_track_writes_its_pin_back(self, rig):
        now = _time.time()
        dep = now + 3 * HOUR
        r = rig(_tracked(pin=None))
        assert "scheduled_departure" not in r.tracked_file()
        with r.airlabs_returns(_sched(dep, dep + 5 * HOUR)):
            r.grab()
        assert r.tracked_file()["scheduled_departure"] == int(dep)

    def test_the_adopted_pin_is_used_from_the_next_poll(self, rig):
        now = _time.time()
        dep = now + 3 * HOUR
        r = rig(_tracked(pin=None))
        with r.airlabs_returns(_sched(dep, dep + 5 * HOUR)):
            r.grab()
            r.o._tracked_schedule_cache.clear()      # force another cold fetch
            r.grab()
        assert r.fetches[0] == ("UA353", None)
        assert r.fetches[-1] == ("UA353", int(dep)), (
            f"the self-pin never reached the next fetch: {r.fetches}")

    def test_a_user_chosen_leg_is_never_overwritten(self, rig):
        """Two ways this must hold: _grab never even asks when a pin exists,
        AND the writer refuses if asked anyway. Only asserting the first leaves
        the guard inside _persist_self_pin free to be deleted — it was, and the
        whole suite stayed green."""
        now = _time.time()
        pin = now + 2 * HOUR
        r = rig(_tracked(pin=pin))
        other = now + 5 * HOUR
        with r.airlabs_returns(_sched(other, other + 5 * HOUR)):
            r.grab()
        assert r.tracked_file()["scheduled_departure"] == pin

        assert r.o._persist_self_pin("UA353", _sched(other, other + 5 * HOUR)) == pin
        assert r.tracked_file()["scheduled_departure"] == pin, (
            "the self-pin overwrote the leg the user actually chose")

    def test_a_flight_swapped_under_us_is_not_pinned(self, rig):
        now = _time.time()
        dep = now + 3 * HOUR
        r = rig(_tracked(pin=None))
        sched = _sched(dep, dep + 5 * HOUR)
        # The user picks a different flight between the fetch and the write-back.
        with open(r.file, "w", encoding="utf-8") as f:
            json.dump(_tracked(callsign="BA117"), f)
        assert r.o._persist_self_pin("UA353", sched) is None
        assert "scheduled_departure" not in r.tracked_file()


class TestStalenessCapSurvivesAnFr24Outage:
    """(E) The cap used to sit behind the zone fetch, inside the try whose
    network handler PRESERVES the last tracked data."""

    def test_the_cap_fires_even_when_fr24_is_down(self, rig):
        now = _time.time()
        r = rig(_tracked(pin=now, set_ts=now - 37 * HOUR))
        r.o._tracked_data = {"callsign": "UAL353", "is_live": True}
        r.o._api.get_flights.side_effect = ConnectionError("FR24 unreachable")
        r.grab()
        assert r.tracked_file()["callsign"] == "", (
            "the 36h cap did not run before the FR24 fetch")
        assert r.o._tracked_data is None, (
            "the panel keeps rendering a flight whose file was wiped")

    def test_a_capped_flight_is_not_polled_for_afterwards(self, rig):
        """The cap runs before the poll reads the file, so the rest of _grab
        sees no tracked flight at all: no AirLabs credit spent on it, and no
        extra cycle of it on the panel."""
        now = _time.time()
        r = rig(_tracked(pin=now + HOUR, set_ts=now - 37 * HOUR))
        with r.airlabs_returns(_sched(now + HOUR, now + 6 * HOUR)):
            r.grab()
        assert r.tracked_file()["callsign"] == ""
        assert r.fetches == [], (
            f"AirLabs was queried for a flight the cap had just wiped: {r.fetches}")
        assert r.o._tracked_data is None

    def test_a_flight_inside_the_cap_survives_an_outage(self, rig):
        now = _time.time()
        r = rig(_tracked(pin=now, set_ts=now - 35 * HOUR))
        r.o._api.get_flights.side_effect = ConnectionError("FR24 unreachable")
        r.grab()
        assert r.tracked_file()["callsign"] == "UA353"

    def test_the_cap_ignores_a_file_with_no_flight(self, rig):
        r = rig({"callsign": "", "set_ts": 0})
        assert r.o._enforce_tracked_age_cap() is False


class TestWasLiveNoEtaRealityCheck:
    """A landed flight whose ETA we lost (position-only cycles carry none, a
    restart drops it) must not be held up forever by a cached schedule that
    rolled to another leg."""

    def _run(self, rig_factory, cached, pin, misses=1):
        now = _time.time()
        r = rig_factory(_tracked(pin=pin))
        r.o._tracked_was_live = True
        r.o._tracked_last_eta = None
        r.o._tracked_last_data = {"callsign": "UAL353", "is_live": True,
                                  "ground_speed": 400, "last_seen_ts": now - 600}
        r.o._tracked_last_callsign = "UA353"
        if cached is not None:
            r.warm(cached)
        for _ in range(misses):
            r.grab()
        return r

    def test_a_next_day_leg_in_the_cache_does_not_stall_the_wipe(self, rig):
        """Without the pin check this arrival is always in the future, so the
        miss counter never advances and the flight never clears."""
        now = _time.time()
        r = self._run(rig, _sched(now + 24 * HOUR, now + 29 * HOUR),
                      pin=now - 8 * HOUR, misses=3)
        assert r.tracked_file()["callsign"] == ""

    def test_the_pinned_leg_still_in_the_air_is_respected(self, rig):
        """The reason this branch exists: an oceanic feed gap must not wipe a
        flight that has not landed yet."""
        now = _time.time()
        pin = now - 2 * HOUR
        r = self._run(rig, _sched(pin, now + 3 * HOUR), pin=pin, misses=5)
        assert r.tracked_file()["callsign"] == "UA353"

    def test_the_pinned_leg_past_its_arrival_wipes(self, rig):
        now = _time.time()
        pin = now - 8 * HOUR
        r = self._run(rig, _sched(pin, now - HOUR), pin=pin, misses=3)
        assert r.tracked_file()["callsign"] == ""

    def test_a_cancelled_leg_wipes(self, rig):
        now = _time.time()
        pin = now - HOUR
        r = self._run(rig, _sched(pin, now + 4 * HOUR, status="cancelled"),
                      pin=pin, misses=3)
        assert r.tracked_file()["callsign"] == ""

    def test_brief_gaps_below_the_threshold_never_wipe(self, rig):
        """_TRACKED_MISS_THRESHOLD exists because FR24 blips must not erase an
        airborne flight."""
        now = _time.time()
        pin = now - 8 * HOUR
        r = self._run(rig, _sched(pin, now - HOUR), pin=pin, misses=2)
        assert r.tracked_file()["callsign"] == "UA353"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Source-level guards for wiring a mutation could quietly remove
# ═══════════════════════════════════════════════════════════════════════════

def _overhead_src():
    return open(os.path.join(os.path.dirname(__file__), "..",
                             "utilities", "overhead.py"), encoding="utf-8").read()


def test_all_three_never_live_fetch_sites_pass_a_pin():
    """Belt and braces for the behavioural tests above: every AirLabs lookup in
    the never-live branch must name pin_dep_ts."""
    src = _overhead_src()
    branch = src[src.index("# Never been live"):src.index("# Departure delay, when AirLabs published one")]
    lookups = branch.count("get_flight_schedule(")
    assert lookups == 2, f"expected 2 direct lookups in the branch, found {lookups}"
    assert branch.count("pin_dep_ts=pin_dep_ts") == 3, (
        "one of the three fetch sites (cold fetch, maybe_refresh, expiry "
        "re-fetch) is not passing the pin")


def test_the_completion_decision_is_actually_consulted():
    src = _overhead_src()
    assert "tracked_completion_decision(" in src
    idx = src.index("_wipe, _why = tracked_completion_decision(")
    after = src[idx:idx + 700]
    assert "self._do_auto_wipe()" in after, (
        "the completion decision is computed but nothing acts on it")


def test_the_age_cap_runs_before_the_fr24_fetch():
    src = _overhead_src()
    cap = src.index("self._enforce_tracked_age_cap()")
    fetch = src.index("flights = self._api.get_flights(")
    assert cap < fetch, (
        "the 36h cap is back behind the FR24 zone fetch, so an outage that "
        "aborts the poll preserves a zombie past the cap")
