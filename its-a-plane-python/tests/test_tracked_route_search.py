"""
test_tracked_route_search.py — Strategy 4, the route-based search.

This exercises _grab_tracked itself with a fake FR24 API. The prefix helper is
unit-tested in test_callsign_prefixes.py, but a review showed that reverting the
whole route-search loop to a single prefix passed all 631 tests: the helper was
covered and the code that USES it was not.

The review also found that widening the prefix list had broken two things that
nothing caught. Both have a test here:

  * the route search used to be gated on AirLabs reporting an operating carrier,
    which was falsy for a plain mainline flight. callsign_prefixes() returns
    [airline_icao] for anything with a schedule, so the search began running for
    EVERY tracked flight — and pre-departure it locked onto whatever
    same-airline aircraft happened to be on the route.
  * a marketing-prefix hit was cached as _tracked_alt_callsign, so Strategy 2
    re-used that wrong callsign on every later poll and Strategy 3 never got to
    find the real operating callsign once it was airborne.
"""

import os
import tempfile

import pytest

os.environ.setdefault("PLANE_TRACKER_DATA_DIR", tempfile.mkdtemp())

import utilities.overhead as ov                      # noqa: E402
from utilities.fr24_client import LiveFlight         # noqa: E402

HOME = ov.LOCATION_DEFAULT
NEAR = (HOME[0], HOME[1])
FAR = (HOME[0] + 5, HOME[1] + 5)


def _flight(callsign, pos):
    return LiveFlight(
        flight_id=callsign, latitude=pos[0], longitude=pos[1], altitude=30000,
        ground_speed=400, heading=90, vertical_speed=0, callsign=callsign,
        registration="", origin_airport_iata="", destination_airport_iata="",
        airline_icao=callsign[:3], airline_iata="", aircraft_code="",
        on_ground=False, eta=0)


class _FakeAPI:
    def __init__(self, live=None, route=None):
        self.live, self.route, self.calls = live or {}, route or [], []

    def find_by_callsign(self, cs):
        self.calls.append(("callsign", cs))
        return self.live.get(cs)

    def find_by_route(self, o, d):
        self.calls.append(("route", o, d))
        return list(self.route)


def _track(flight_input, sched, route=(), live=None, alt=""):
    """Drive _grab_tracked and return (matched_callsign, alt_lock, overhead)."""
    o = object.__new__(ov.Overhead)
    o._api = _FakeAPI(live or {}, list(route))
    o._tracked_schedule_cache = {flight_input: sched} if sched else {}
    o._tracked_alt_callsign = alt
    o._tracked_route_cached = None
    r = o._grab_tracked(flight_input, zone_flights=None, update_position_only=True)
    return (r["callsign"] if r else None), o._tracked_alt_callsign, o


LX561 = {"flight_iata": "LX561", "flight_icao": "SWR561", "airline_iata": "LX",
         "airline_icao": "SWR", "cs_airline_iata": "2L", "cs_flight_iata": "2L561",
         "origin": "NCE", "destination": "ZRH"}
UA353 = {"flight_iata": "UA353", "flight_icao": "UAL353", "airline_iata": "UA",
         "airline_icao": "UAL", "cs_airline_iata": "", "origin": "EWR",
         "destination": "LAX"}
AA4370 = {"flight_iata": "AA4370", "flight_icao": "AAL4370", "airline_iata": "AA",
          "airline_icao": "AAL", "cs_airline_iata": "OH", "cs_flight_iata": "OH4370",
          "origin": "DCA", "destination": "CLT"}


class TestTheReportedBug:
    def test_a_decoupled_callsign_is_found_by_route(self):
        """LX561 flies as SWR1PX — the MARKETING carrier's prefix, no flight
        number in the callsign. Filtering only by the operator (2L/OAW) could
        never match it. This is the whole point of the change."""
        match, _, _ = _track("LX561", LX561, route=[_flight("SWR1PX", FAR)])
        assert match == "SWR1PX"

    def test_a_same_airline_flight_with_a_different_number_is_rejected(self):
        """SWR40 is provably not LX561, and it is NEARER home — so the
        nearest-home tie-break would have picked it."""
        match, _, _ = _track("LX561", LX561,
                             route=[_flight("SWR1PX", FAR), _flight("SWR40", NEAR)])
        assert match == "SWR1PX", "picked a same-airline aircraft we know is another flight"

    def test_the_operating_carrier_still_wins_when_it_is_flying(self):
        match, _, _ = _track("LX561", LX561,
                             route=[_flight("SWR1PX", NEAR), _flight("OAW77", FAR)])
        assert match == "OAW77"

    def test_a_marketing_prefix_guess_is_not_cached_as_the_alt_callsign(self):
        """Caching it makes Strategy 2 re-use the guess forever and stops
        Strategy 3 finding the real operating callsign."""
        _, alt, _ = _track("LX561", LX561, route=[_flight("SWR1PX", FAR)])
        assert alt == "", f"marketing-prefix hit was cached as {alt!r}"


class TestNoPreDepartureFalseMatch:
    def test_a_plain_mainline_flight_does_not_match_a_sibling(self):
        """UA353 has no codeshare. Before the fix the route search ran anyway
        and returned whatever UAL was on EWR-LAX — there is nearly always one."""
        match, alt, _ = _track("UA353", UA353, route=[_flight("UAL1234", FAR)])
        assert match is None, f"matched an unrelated aircraft: {match}"
        assert alt == ""

    def test_a_regional_codeshare_does_not_match_the_mainline(self):
        """AA4370 is operated by JIA. Pre-departure JIA4370 is not airborne, so
        the loop fell through to the marketing prefix and took AAL1000."""
        match, _, _ = _track("AA4370", AA4370, route=[_flight("AAL1000", FAR)])
        assert match is None, f"matched the mainline aircraft: {match}"

    def test_and_still_finds_the_regional_once_it_is_airborne(self):
        """The guard must not cost us the case the route search exists for."""
        match, alt, _ = _track("AA4370", AA4370,
                               route=[_flight("AAL1000", NEAR), _flight("JIA4370", FAR)])
        assert match == "JIA4370"
        assert alt == "JIA4370", "an operating-carrier hit SHOULD be cached"

    def test_the_wrong_match_does_not_become_sticky(self):
        """Full sequence: a bad pre-departure match used to be cached, and
        Strategy 2 then returned it on every later poll even once the real
        aircraft was up."""
        _, alt, _ = _track("AA4370", AA4370, route=[_flight("AAL1000", FAR)])
        match, _, _ = _track("AA4370", AA4370, alt=alt,
                             live={"JIA4370": _flight("JIA4370", NEAR),
                                   "AAL1000": _flight("AAL1000", FAR)})
        assert match == "JIA4370", f"stuck on a stale alt-callsign: {match}"


class TestPrefixLooseness:
    def test_a_two_letter_iata_prefix_requires_a_digit_after_it(self):
        """Air Dolomiti's IATA is EN, which also prefixes Enter Air's ENT123.
        A flight number always follows the IATA code with a digit; another
        airline's ICAO never does."""
        sched = {"flight_iata": "EN8266", "airline_iata": "EN", "airline_icao": "DLA",
                 "cs_airline_iata": "EN", "origin": "MUC", "destination": "VRN"}
        match, _, _ = _track("EN8266", sched,
                             route=[_flight("ENT123", NEAR), _flight("DLH9", FAR)])
        assert match is None, f"matched another airline via a 2-letter prefix: {match}"


class TestNoSchedule:
    def test_without_a_schedule_there_is_no_route_to_search(self):
        match, _, o = _track("LX561", None, route=[_flight("SWR1PX", FAR)])
        assert match is None
        assert not any(c[0] == "route" for c in o._api.calls)


class TestDepartureGate:
    """The gate is separate from the different-number rejection, and only this
    covers it: here the sibling's number MATCHES the tracked flight, so the
    number rule cannot help and the gate is the only thing standing between a
    not-yet-departed flight and a wrong aircraft.

    Real shape of it: two aircraft can carry the same flight number on the same
    route — yesterday's rotation still airborne, or a delayed earlier leg.
    """

    @staticmethod
    def _sched(dep_ts):
        return dict(LX561, dep_time_ts=dep_ts, dep_time="2026-08-21 10:00")

    def test_before_departure_the_route_is_not_searched(self):
        import time as _t
        match, _, o = _track("LX561", self._sched(_t.time() + 3600),
                             route=[_flight("SWR1PX", FAR)])
        assert match is None, f"matched before the flight was due out: {match}"
        assert not any(c[0] == "route" for c in o._api.calls), \
            "the route search ran before departure"

    def test_after_departure_it_is(self):
        import time as _t
        match, _, o = _track("LX561", self._sched(_t.time() - 600),
                             route=[_flight("SWR1PX", FAR)])
        assert match == "SWR1PX"
        assert any(c[0] == "route" for c in o._api.calls)
