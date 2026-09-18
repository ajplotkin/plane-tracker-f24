"""The /tracked/lookup contract the leg picker is written against.

The picker's JS is covered by tests/test_leg_picker_js.py, which drives the
page against stubbed responses. That leaves one seam uncovered: whether the
server actually SENDS the shape the page reads. It did not, once — the picker
was supposed to name each leg's airline and the field was never in the payload,
so every button rendered the same two-airport line.
"""

import os
import sys
from time import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _leg(org, dst, dep_off, icao, **over):
    d = {"origin": org, "destination": dst, "dep_time": "07:29",
         "status": "scheduled", "dep_time_ts": time() + dep_off,
         "airline_icao": icao, "duration": 120}
    d.update(over)
    return d


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANE_TRACKER_DATA_DIR", str(tmp_path))
    import web.app as app_mod
    return app_mod, app_mod.app.test_client()


def _lookup(app_mod, client, legs, sched=None):
    """POST a lookup with FR24 offline, so the AirLabs multi-leg path runs."""
    api = MagicMock()
    api.find_by_callsign.return_value = None          # not airborne
    with patch.object(app_mod, "_fr24_client", api), \
         patch("utilities.airlabs.get_flight_legs", return_value=legs), \
         patch("utilities.airlabs.get_flight_schedule",
               return_value=sched if sched is not None else legs[0]):
        return client.post("/tracked/lookup", json={"callsign": "UA1714"}).get_json()


class TestMultiLegLookupPayload:
    LEGS = [_leg("LGA", "DEN", 3600, "UAL"), _leg("DEN", "GJT", 5 * 3600, "SKW")]

    def test_several_legs_are_returned_for_the_user_to_choose(self, client):
        app_mod, c = client
        d = _lookup(app_mod, c, self.LEGS)
        assert d["multiple"] is True
        assert len(d["flights"]) == 2

    def test_each_leg_names_the_airline_that_flies_it(self, client):
        """The page renders `lg.airline_name`. Two legs of one flight number
        are routinely flown by different carriers — here mainline then a
        regional — and without this they are indistinguishable on the button.
        """
        app_mod, c = client
        d = _lookup(app_mod, c, self.LEGS)
        names = [f.get("airline_name") for f in d["flights"]]
        assert all(names), f"a leg came back with no airline_name: {names}"
        assert names[0] != names[1], (
            "both legs named the same airline — the lookup is not reading each "
            "leg's own carrier")

    def test_each_leg_carries_what_pins_it(self, client):
        """cached_route and scheduled_departure are the only things that tell
        the two legs apart once they are saved; both share a callsign."""
        app_mod, c = client
        d = _lookup(app_mod, c, self.LEGS)
        for f in d["flights"]:
            assert f["cached_route"]["origin"] == f["origin"]
            assert f["cached_route"]["destination"] == f["destination"]
            assert f["scheduled_departure"], "leg has no pin"
        assert (d["flights"][0]["scheduled_departure"]
                != d["flights"][1]["scheduled_departure"])
