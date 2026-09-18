"""Tests for AirLabs schedule lookup and pre-departure tracked flight handling."""

import os
import sys
import types
from time import time
from unittest.mock import patch, MagicMock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock rgbmatrix so we can import scenes/trackedstats.py without Pi hardware
if "rgbmatrix" not in sys.modules:
    _mock_rgbmatrix = types.ModuleType("rgbmatrix")
    _mock_rgbmatrix.graphics = MagicMock()
    _mock_rgbmatrix.RGBMatrix = MagicMock
    _mock_rgbmatrix.RGBMatrixOptions = MagicMock
    sys.modules["rgbmatrix"] = _mock_rgbmatrix


class TestAirLabsModule:
    """Test utilities/airlabs.py."""

    def setup_method(self):
        """Clear module cache between tests."""
        import utilities.airlabs as mod
        mod._cache.clear()

    def test_get_flight_schedule_success(self):
        """Successful schedule lookup returns parsed dict."""
        import utilities.airlabs as mod
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "response": [{
                "dep_iata": "EWR",
                "arr_iata": "LAX",
                "dep_time": "2026-05-11 18:30",
                "dep_time_utc": "2026-05-11 22:30",
                "dep_time_ts": time() + 3600,  # 1 hour from now
                "arr_time": "2026-05-11 21:45",
                "arr_time_utc": "2026-05-12 01:45",
                "status": "scheduled",
                "airline_iata": "UA",
                "flight_iata": "UA353",
                "duration": 330,
            }]
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(mod, 'AIRLABS_API_KEY', 'test-key'):
            with patch('utilities.airlabs.requests.get', return_value=mock_response) as mock_get:
                result = mod.get_flight_schedule("UA353")

        assert result is not None
        assert result["origin"] == "EWR"
        assert result["destination"] == "LAX"
        assert result["status"] == "scheduled"
        assert result["flight_number"] == "UA353"
        # Verify correct endpoint called
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "schedules" in call_args[0][0]
        assert call_args[1]["params"]["flight_iata"] == "UA353"

    def test_get_flight_schedule_icao_format(self):
        """ICAO callsign (UAL353) uses flight_icao param."""
        import utilities.airlabs as mod
        mock_response = MagicMock()
        mock_response.json.return_value = {"response": []}
        mock_response.raise_for_status = MagicMock()

        with patch.object(mod, 'AIRLABS_API_KEY', 'test-key'):
            with patch('utilities.airlabs.requests.get', return_value=mock_response) as mock_get:
                mod.get_flight_schedule("UAL353")

        call_args = mock_get.call_args
        assert call_args[1]["params"]["flight_icao"] == "UAL353"

    def test_get_flight_schedule_no_key(self):
        """Returns None when no API key configured."""
        import utilities.airlabs as mod
        with patch.object(mod, 'AIRLABS_API_KEY', ''):
            result = mod.get_flight_schedule("UA353")
        assert result is None

    def test_get_flight_schedule_empty_response(self):
        """Returns None when API returns no schedules."""
        import utilities.airlabs as mod
        mock_response = MagicMock()
        mock_response.json.return_value = {"response": []}
        mock_response.raise_for_status = MagicMock()

        with patch.object(mod, 'AIRLABS_API_KEY', 'test-key'):
            with patch('utilities.airlabs.requests.get', return_value=mock_response):
                result = mod.get_flight_schedule("FAKE999")
        assert result is None

    def test_get_flight_schedule_picks_upcoming(self):
        """When multiple segments returned, picks the next upcoming one."""
        import utilities.airlabs as mod
        now = time()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "response": [
                {
                    "dep_iata": "LAX", "arr_iata": "ORD",
                    "dep_time": "2026-05-11 22:00", "dep_time_utc": "2026-05-12 05:00",
                    "dep_time_ts": now + 7200,  # 2 hours from now (later segment)
                    "arr_time": "2026-05-12 04:00", "arr_time_utc": "2026-05-12 08:00",
                    "status": "scheduled", "airline_iata": "UA", "flight_iata": "UA353",
                    "duration": 240,
                },
                {
                    "dep_iata": "EWR", "arr_iata": "LAX",
                    "dep_time": "2026-05-11 18:30", "dep_time_utc": "2026-05-11 22:30",
                    "dep_time_ts": now + 3600,  # 1 hour from now (earlier segment)
                    "arr_time": "2026-05-11 21:45", "arr_time_utc": "2026-05-12 01:45",
                    "status": "scheduled", "airline_iata": "UA", "flight_iata": "UA353",
                    "duration": 330,
                },
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(mod, 'AIRLABS_API_KEY', 'test-key'):
            with patch('utilities.airlabs.requests.get', return_value=mock_response):
                result = mod.get_flight_schedule("UA353")

        # Should pick EWR→LAX (earlier dep_time_ts)
        assert result["origin"] == "EWR"
        assert result["destination"] == "LAX"

    def test_get_flight_schedule_network_error(self):
        """Returns None on network error."""
        import utilities.airlabs as mod
        import requests as req

        with patch.object(mod, 'AIRLABS_API_KEY', 'test-key'):
            with patch('utilities.airlabs.requests.get', side_effect=req.exceptions.Timeout):
                result = mod.get_flight_schedule("UA353")
        assert result is None

    def test_empty_callsign(self):
        """Returns None for empty callsign."""
        import utilities.airlabs as mod
        assert mod.get_flight_schedule("") is None
        assert mod.get_flight_schedule("  ") is None


class TestFormatDepTime:
    """Test _format_dep_time imported from scenes/trackedstats.py."""

    def _format_12hr(self, dep_time_str):
        import scenes.trackedstats as ts
        ts.CLOCK_FORMAT = "12hr"
        return ts._format_dep_time(dep_time_str)

    def _format_24hr(self, dep_time_str):
        import scenes.trackedstats as ts
        ts.CLOCK_FORMAT = "24hr"
        return ts._format_dep_time(dep_time_str)

    def test_afternoon_12hr(self):
        assert self._format_12hr("2026-05-11 18:30") == "6:30p"

    def test_morning_12hr(self):
        assert self._format_12hr("2026-05-11 09:15") == "9:15a"

    def test_noon_12hr(self):
        assert self._format_12hr("2026-05-11 12:00") == "12p"

    def test_midnight_12hr(self):
        assert self._format_12hr("2026-05-11 00:00") == "12a"

    def test_on_the_hour_12hr(self):
        assert self._format_12hr("2026-05-11 14:00") == "2p"

    def test_afternoon_24hr(self):
        assert self._format_24hr("2026-05-11 18:30") == "18:30"

    def test_morning_24hr(self):
        assert self._format_24hr("2026-05-11 09:15") == "9:15"

    def test_midnight_24hr(self):
        assert self._format_24hr("2026-05-11 00:00") == "0:00"

    def test_empty(self):
        assert self._format_12hr("") == ""

    def test_none(self):
        assert self._format_12hr(None) == ""


class TestGetFlightLegsWindow:
    """Which legs survive the filter.

    The original rule was `dep_time_ts > now - 3600`: a leg vanished 60 minutes
    after pushback. A transcon three hours from its destination therefore
    dropped out of the picker while very much in the air — and reading that
    absence as "it landed" is exactly the wrong conclusion to hand a user.
    Ordered so a naive implementation fails: the long-airborne leg is FIRST,
    so "return everything" and "return the tail" both look right otherwise.
    """

    def _legs(self, schedules):
        import utilities.airlabs as mod
        resp = MagicMock()
        resp.json.return_value = {"response": schedules}
        resp.raise_for_status = MagicMock()
        with patch.object(mod, "AIRLABS_API_KEY", "test-key"):
            with patch("utilities.airlabs.requests.get", return_value=resp):
                return mod.get_flight_legs("UA1714")

    def _leg(self, dep_off, arr_off, org, dst, **over):
        now = time()
        d = {"dep_iata": org, "arr_iata": dst, "flight_iata": "UA1714",
             "dep_time_ts": now + dep_off, "arr_time_ts": now + arr_off,
             "status": "en-route"}
        d.update(over)
        return d

    def test_a_leg_airborne_three_hours_is_kept(self):
        legs = self._legs([
            self._leg(-2 * 3600, +3600, "LGA", "DEN"),   # departed 2h ago, lands in 1h
            self._leg(+4 * 3600, +6 * 3600, "DEN", "GJT"),
        ])
        routes = [(l["origin"], l["destination"]) for l in legs]
        assert ("LGA", "DEN") in routes, (
            "a leg still an hour from its destination was dropped — the "
            "departure-based window is back")
        assert ("DEN", "GJT") in routes

    def test_a_leg_that_has_arrived_is_dropped(self):
        legs = self._legs([
            self._leg(-6 * 3600, -2 * 3600, "SFO", "LGA"),   # landed 2h ago
            self._leg(+4 * 3600, +6 * 3600, "DEN", "GJT"),
        ])
        routes = [(l["origin"], l["destination"]) for l in legs]
        assert ("SFO", "LGA") not in routes, "a leg that already landed was offered"
        assert ("DEN", "GJT") in routes

    def test_without_an_arrival_time_it_falls_back_to_the_departure_window(self):
        """AirLabs does not always send arr_time_ts; the old rule is the
        fallback, not the primary."""
        legs = self._legs([
            self._leg(-30 * 60, 0, "LGA", "DEN", arr_time_ts=None),   # left 30m ago
            self._leg(-5 * 3600, 0, "SFO", "LGA", arr_time_ts=None),  # left 5h ago
        ])
        routes = [(l["origin"], l["destination"]) for l in legs]
        assert ("LGA", "DEN") in routes
        assert ("SFO", "LGA") not in routes

    def test_each_leg_carries_what_the_picker_needs_to_name_its_airline(self):
        """_build_cached_route looks the carrier name up from airline_icao;
        without it every picked leg showed a blank airline."""
        legs = self._legs([self._leg(+3600, +3 * 3600, "DEN", "GJT",
                                     airline_icao="SKW", airline_iata="OO")])
        assert legs[0]["airline_icao"] == "SKW"
        assert legs[0]["arr_time_ts"] is not None
