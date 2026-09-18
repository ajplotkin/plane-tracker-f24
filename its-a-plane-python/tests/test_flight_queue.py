"""Queueing a connection: track this leg now, switch when it is over.

The device shows ONE flight. A queue makes the next leg of an itinerary take
over automatically when the current one finishes, so a connection does not have
to be re-entered mid-trip.

The handover is driven by COMPLETION, never by a clock. Two legs of one
itinerary cannot be wanted at the same time — you cannot board the second until
the first lands — so a delay postpones the handover instead of racing it. Every
completion path in the tracker funnels through _do_auto_wipe(), which is why
that is the only place this had to change.
"""

import json
import os
import sys
import tempfile
from time import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# overhead.py makes DATA_DIR at import time, which on a dev box is a path only
# the Pi has. Same preamble as tests/test_leg_pinning.py.
os.environ.setdefault("ZONE_TL_LAT", "51.7")
os.environ.setdefault("ZONE_TL_LON", "-0.3")
os.environ.setdefault("ZONE_BR_LAT", "51.47")
os.environ.setdefault("ZONE_BR_LON", "-0.111")
os.environ.setdefault("HOME_LAT", "51.55864")
os.environ.setdefault("HOME_LON", "-0.177332")
os.environ.setdefault("DISTANCE_UNITS", "imperial")
os.environ.setdefault("PLANE_TRACKER_DATA_DIR", tempfile.mkdtemp())

import utilities.overhead as overhead   # noqa: E402


HOUR = 3600


def _leg(callsign, org, dst, dep):
    return {"callsign": callsign, "scheduled_departure": dep,
            "cached_route": {"origin": org, "destination": dst}}


@pytest.fixture
def tracked(tmp_path, monkeypatch):
    """An Overhead whose tracked file we can read back after a completion."""
    path = os.path.join(str(tmp_path), "tracked_flight.json")
    monkeypatch.setattr(overhead, "TRACKED_FILE", path)
    monkeypatch.setattr(overhead, "DATA_DIR", str(tmp_path))

    def _make(doc):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        o = overhead.Overhead()
        o._api = MagicMock()
        return o, path

    return _make


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestAdvancingThroughAQueue:

    def test_completion_promotes_the_next_leg(self, tracked):
        now = time()
        o, path = tracked({
            "callsign": "UA1714", "set_ts": int(now),
            "scheduled_departure": now - 3 * HOUR,
            "cached_route": {"origin": "LGA", "destination": "DEN"},
            "queue": [_leg("UA1714", "DEN", "GJT", now + HOUR)],
        })
        o._do_auto_wipe()
        d = _read(path)
        assert d["callsign"] == "UA1714"
        assert d["cached_route"] == {"origin": "DEN", "destination": "GJT"}, (
            "the finished leg's route was left in place")
        assert d["scheduled_departure"] == now + HOUR
        assert d["queue"] == [], "the promoted leg was not removed from the queue"

    def test_the_promoted_leg_keeps_its_own_pin(self, tracked):
        """Losing the pin would let AirLabs substitute another leg of the same
        flight number — the exact failure leg pinning exists to stop."""
        now = time()
        o, path = tracked({
            "callsign": "AA100", "set_ts": int(now),
            "queue": [_leg("AA100", "LHR", "JFK", now + 5 * HOUR)],
        })
        o._do_auto_wipe()
        assert _read(path)["scheduled_departure"] == now + 5 * HOUR

    def test_a_three_leg_itinerary_advances_one_at_a_time(self, tracked):
        now = time()
        o, path = tracked({
            "callsign": "UA1", "set_ts": int(now),
            "queue": [_leg("UA2", "DEN", "GJT", now + HOUR),
                      _leg("UA3", "GJT", "LAX", now + 4 * HOUR)],
        })
        o._do_auto_wipe()
        d = _read(path)
        assert d["callsign"] == "UA2"
        assert len(d["queue"]) == 1, "advanced past more than one leg"
        o._do_auto_wipe()
        d = _read(path)
        assert d["callsign"] == "UA3"
        assert d["queue"] == []

    def test_an_empty_queue_still_clears_as_before(self, tracked):
        o, path = tracked({"callsign": "UA1714", "set_ts": int(time()), "queue": []})
        o._do_auto_wipe()
        assert _read(path)["callsign"] == ""

    def test_no_queue_key_at_all_still_clears(self, tracked):
        """Files written before queues existed must behave exactly as they did."""
        o, path = tracked({"callsign": "UA1714", "set_ts": int(time())})
        o._do_auto_wipe()
        assert _read(path)["callsign"] == ""

    def test_a_queued_leg_without_a_pin_is_not_given_a_null_one(self, tracked):
        """A missing pin means 'track blind'; a null one would look deliberate
        and defeat the self-pin that adopts the resolved leg."""
        o, path = tracked({
            "callsign": "UA1", "set_ts": int(time()),
            "queue": [{"callsign": "UA2"}],
        })
        o._do_auto_wipe()
        d = _read(path)
        assert d["callsign"] == "UA2"
        assert "scheduled_departure" not in d
        assert "cached_route" not in d

    def test_advancing_resets_the_tracker_state(self, tracked):
        """The new leg must not inherit the finished leg's position or ETA."""
        now = time()
        o, path = tracked({
            "callsign": "UA1", "set_ts": int(now),
            "queue": [_leg("UA2", "DEN", "GJT", now + HOUR)],
        })
        o._tracked_was_live = True
        o._tracked_last_eta = now + 600
        o._tracked_last_data = {"callsign": "UAL1", "is_live": True}
        o._tracked_route_cached = {"origin": "LGA", "destination": "DEN"}
        o._do_auto_wipe()
        assert o._tracked_was_live is False
        assert o._tracked_last_eta is None
        assert o._tracked_last_data is None
        assert o._tracked_route_cached is None

    def test_an_unreadable_tracked_file_still_clears(self, tracked):
        o, path = tracked({"callsign": "UA1", "set_ts": int(time())})
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        o._do_auto_wipe()
        assert _read(path)["callsign"] == "", "a corrupt file blocked the clear"


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Flask test client whose TRACKED_FILE is a scratch path."""
    monkeypatch.setenv("PLANE_TRACKER_DATA_DIR", str(tmp_path))
    import web.app as app_mod
    path = os.path.join(str(tmp_path), "tracked_flight.json")
    monkeypatch.setattr(app_mod, "TRACKED_FILE", path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"callsign": "UA1714", "set_ts": int(time())}, f)
    return app_mod.app.test_client(), path


class TestTheQueueApi:

    def test_a_leg_can_be_queued(self, api):
        c, path = api
        r = c.post("/tracked/queue", json={
            "callsign": "UA1714",
            "cached_route": {"origin": "DEN", "destination": "GJT"},
            "scheduled_departure": 12345})
        assert r.status_code == 200
        q = _read(path)["queue"]
        assert len(q) == 1
        assert q[0]["cached_route"]["destination"] == "GJT"
        assert q[0]["scheduled_departure"] == 12345

    def test_queueing_does_not_disturb_the_current_flight(self, api):
        c, path = api
        c.post("/tracked/queue", json={"callsign": "UA99"})
        assert _read(path)["callsign"] == "UA1714"

    def test_legs_queue_in_the_order_added(self, api):
        c, path = api
        for cs in ("UA2", "UA3", "UA4"):
            c.post("/tracked/queue", json={"callsign": cs})
        assert [l["callsign"] for l in _read(path)["queue"]] == ["UA2", "UA3", "UA4"]

    def test_changing_the_current_flight_keeps_the_queue(self, api):
        """The queue is what comes NEXT; re-picking what is on screen now must
        not throw it away."""
        c, path = api
        c.post("/tracked/queue", json={"callsign": "UA2"})
        c.post("/tracked/set", json={"callsign": "DL500"})
        d = _read(path)
        assert d["callsign"] == "DL500"
        assert [l["callsign"] for l in d["queue"]] == ["UA2"]

    def test_clearing_tracking_clears_the_queue_too(self, api):
        """An empty callsign means stop — leaving a queue armed would restart
        tracking by itself."""
        c, path = api
        c.post("/tracked/queue", json={"callsign": "UA2"})
        c.post("/tracked/set", json={"callsign": ""})
        assert not _read(path).get("queue")

    def test_a_queued_leg_can_be_removed(self, api):
        c, path = api
        for cs in ("UA2", "UA3"):
            c.post("/tracked/queue", json={"callsign": cs})
        r = c.post("/tracked/queue/remove", json={"index": 0})
        assert r.status_code == 200
        assert [l["callsign"] for l in _read(path)["queue"]] == ["UA3"]

    def test_removing_a_leg_that_is_not_there_is_rejected(self, api):
        c, path = api
        c.post("/tracked/queue", json={"callsign": "UA2"})
        assert c.post("/tracked/queue/remove", json={"index": 5}).status_code == 400
        assert c.post("/tracked/queue/remove", json={"index": -1}).status_code == 400
        assert len(_read(path)["queue"]) == 1, "a bad index still mutated the queue"

    def test_an_empty_callsign_cannot_be_queued(self, api):
        c, path = api
        assert c.post("/tracked/queue", json={"callsign": "  "}).status_code == 400
        assert not _read(path).get("queue")

    def test_the_queue_is_capped(self, api):
        """Unbounded, a stuck client could grow the file without limit."""
        c, path = api
        for i in range(8):
            assert c.post("/tracked/queue", json={"callsign": f"UA{i}"}).status_code == 200
        assert c.post("/tracked/queue", json={"callsign": "UA9"}).status_code == 400
        assert len(_read(path)["queue"]) == 8

    def test_the_queue_is_visible_to_the_page(self, api):
        c, path = api
        c.post("/tracked/queue", json={"callsign": "UA2"})
        d = c.get("/tracked/json").get_json()
        assert [l["callsign"] for l in d["queue"]] == ["UA2"]

    def test_queueing_with_nothing_tracked_starts_it_now(self, api):
        """There is no completion to wait for, so a queued leg would sit there
        forever. "Next" with nothing current means now."""
        c, path = api
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"callsign": "", "set_ts": 0}, f)
        r = c.post("/tracked/queue", json={
            "callsign": "UA1714",
            "cached_route": {"origin": "DEN", "destination": "GJT"},
            "scheduled_departure": 999})
        assert r.get_json().get("started_now") is True
        d = _read(path)
        assert d["callsign"] == "UA1714"
        assert d["cached_route"]["destination"] == "GJT"
        assert d["scheduled_departure"] == 999
        assert d["queue"] == []

    def test_starting_now_does_not_discard_an_existing_queue(self, api):
        c, path = api
        c.post("/tracked/queue", json={"callsign": "UA2"})
        doc = _read(path)
        doc["callsign"] = ""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        c.post("/tracked/queue", json={"callsign": "UA1"})
        d = _read(path)
        assert d["callsign"] == "UA1"
        assert [l["callsign"] for l in d["queue"]] == ["UA2"]
