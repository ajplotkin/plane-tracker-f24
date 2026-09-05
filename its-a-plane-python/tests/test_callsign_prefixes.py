"""
test_callsign_prefixes.py — finding a flight whose ATC callsign is decoupled
from its ticketed flight number.

Reported from a real trip: LX561 NCE->ZRH was entered into the tracker and never
appeared, though the aircraft was overhead. AirLabs says LX561 is marketed by
Swiss (LX/SWR) and OPERATED by Helvetic Airways (2L/OAW), and reports its ICAO
code as SWR561 — but the aircraft actually flew as SWR1PX, a callsign containing
no flight number at all, which AirLabs has no record of.

European carriers routinely fly alphanumeric callsigns decoupled from the
ticketed number so that similar-sounding callsigns stay off one frequency. US
carriers mostly still fly UAL1234 for UA1234, which is why this never showed up
in domestic testing.

The route search is the only strategy that can survive that — but it filtered on
the OPERATING carrier, looking for OAW while the aircraft squawked SWR.
"""

import os
import tempfile

# utilities.overhead resolves DATA_DIR at IMPORT time and defaults to
# /var/lib/plane-tracker, which is root-owned on a dev box. Point it somewhere
# writable first, exactly as tests/test_leg_pinning.py does.
os.environ.setdefault("PLANE_TRACKER_DATA_DIR", tempfile.mkdtemp())

from utilities.overhead import IATA_TO_ICAO, callsign_prefixes  # noqa: E402


class TestPrefixOrder:
    def test_the_marketing_carrier_is_tried(self):
        """THE BUG: without the marketing carrier's ICAO there is no prefix that
        can ever match SWR1PX, so LX561 is unfindable by route."""
        prefixes = callsign_prefixes({
            "cs_airline_iata": "2L", "airline_icao": "SWR", "airline_iata": "LX",
        })
        assert "SWR" in prefixes, prefixes

    def test_the_operating_carrier_still_comes_first(self):
        """US regionals really do fly under their own callsign — AA4370 flies as
        JIA4370 — so the operator must stay ahead of the marketing carrier or
        this change would regress every domestic codeshare."""
        prefixes = callsign_prefixes({
            "cs_airline_iata": "OH", "airline_icao": "AAL", "airline_iata": "AA",
        })
        assert prefixes[0] == "JIA", prefixes
        assert prefixes.index("JIA") < prefixes.index("AAL")

    def test_the_operating_iata_is_kept_when_no_icao_is_known(self):
        prefixes = callsign_prefixes({
            "cs_airline_iata": "ZZ", "airline_icao": "SWR",
        })
        assert prefixes == ["ZZ", "SWR"], prefixes

    def test_no_duplicates(self):
        """A flight operated by its own marketing carrier resolves the operator
        ICAO and the marketing ICAO to the SAME value; it must appear once, or
        the route is filtered twice for nothing. The operator's IATA is still a
        distinct candidate and is kept."""
        prefixes = callsign_prefixes({
            "cs_airline_iata": "LX", "airline_icao": "SWR",
        })
        assert prefixes == ["SWR", "LX"], prefixes
        assert len(prefixes) == len(set(prefixes))

    def test_empty_and_missing_fields_are_safe(self):
        assert callsign_prefixes(None) == []
        assert callsign_prefixes({}) == []
        assert callsign_prefixes({"cs_airline_iata": "", "airline_icao": ""}) == []


class TestEuropeanCodes:
    def test_swiss_and_its_wet_lease_operators_are_mapped(self):
        """None of these were in the map, which is the other half of why LX561
        failed: the prefix swap had nothing to swap to."""
        assert IATA_TO_ICAO.get("LX") == "SWR"   # Swiss
        assert IATA_TO_ICAO.get("2L") == "OAW"   # Helvetic — operates LX561
        assert IATA_TO_ICAO.get("BT") == "BTI"   # airBaltic — also ACMI for Swiss

    def test_the_map_did_not_lose_its_us_entries(self):
        for iata, icao in (("UA", "UAL"), ("AA", "AAL"), ("DL", "DAL"),
                           ("YX", "RPA"), ("OH", "JIA")):
            assert IATA_TO_ICAO.get(iata) == icao, iata


class TestTheReportedFlight:
    def test_lx561_now_yields_a_prefix_that_matches_the_real_callsign(self):
        """End to end on the real numbers: the schedule AirLabs returns for
        LX561 must produce a prefix that SWR1PX actually starts with."""
        sched = {                      # verbatim from a live AirLabs response
            "flight_iata": "LX561", "flight_icao": "SWR561",
            "airline_iata": "LX", "airline_icao": "SWR",
            "cs_airline_iata": "2L", "cs_flight_iata": "2L561",
            "origin": "NCE", "destination": "ZRH",
        }
        prefixes = callsign_prefixes(sched)
        assert any("SWR1PX".startswith(p) for p in prefixes), (
            f"none of {prefixes} matches the callsign that actually flew")

    def test_the_flight_number_alone_would_never_have_matched(self):
        """Guards against 'just search for the number'. SWR1PX contains no 561,
        so any strategy keyed on the flight number is hopeless here."""
        assert "561" not in "SWR1PX"
        assert not "SWR1PX".startswith("SWR561")
