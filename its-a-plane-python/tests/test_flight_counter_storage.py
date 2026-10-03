"""The flight counter must not stall the panel.

It used to be one JSON file of every day's flights, re-read in full on every
call — once per flight per cycle — and rewritten in full for each new flight.
At 6.3 MB that was 300-360 ms to parse plus ~200 ms to write on a Pi, all with
the GIL held, so the render thread froze for that long. These tests pin the
replacement: per-day files, today's callsigns in memory, and a one-time split
of the old file that loses nothing.
"""

import builtins
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ZONE_TL_LAT", "51.7")
os.environ.setdefault("ZONE_TL_LON", "-0.3")
os.environ.setdefault("ZONE_BR_LAT", "51.47")
os.environ.setdefault("ZONE_BR_LON", "-0.111")
os.environ.setdefault("HOME_LAT", "51.55864")
os.environ.setdefault("HOME_LON", "-0.177332")
os.environ.setdefault("PLANE_TRACKER_DATA_DIR", tempfile.mkdtemp())

import utilities.overhead as oh   # noqa: E402

TODAY = str(datetime.now().date())


def _day(n_ago):
    return str((datetime.now() - timedelta(days=n_ago)).date())


@pytest.fixture
def counter(tmp_path, monkeypatch):
    path = str(tmp_path / "flight_counter.json")
    monkeypatch.setattr(oh, "COUNTER_FILE", path)
    oh._counter_mem.update(path=None, day=None, doc=None, seen=set())
    yield path
    oh._counter_mem.update(path=None, day=None, doc=None, seen=set())


def _opens(fn):
    """Count file opens made while running fn()."""
    real = builtins.open
    calls = []

    def spy(file, *a, **k):
        calls.append((str(file), (a[0] if a else k.get("mode", "r"))))
        return real(file, *a, **k)
    with patch.object(builtins, "open", spy):
        fn()
    return calls


class TestNoFileIoForAFlightAlreadyCounted:

    def test_a_repeat_flight_touches_no_file(self, counter):
        """The hot path. Every cycle re-logs every flight still overhead; that
        must be a set lookup, not a parse of the whole history."""
        oh.log_flight_count("UAL1", {})
        calls = _opens(lambda: oh.log_flight_count("UAL1", {}))
        assert calls == [], f"a repeat flight did file I/O: {calls}"

    def test_a_new_flight_writes_only_todays_file(self, counter):
        oh.log_flight_count("UAL1", {})
        calls = _opens(lambda: oh.log_flight_count("DAL2", {}))
        written = [f for f, m in calls if "w" in m]
        assert written, "the new flight was not persisted"
        assert all(os.path.basename(f).startswith(TODAY) for f in written), written
        read = [f for f, m in calls if "r" in m and "w" not in m]
        assert read == [], f"a new flight re-read files it already holds: {read}"

    def test_the_old_whole_history_file_is_never_rewritten(self, counter):
        oh.log_flight_count("UAL1", {})
        oh.log_flight_count("DAL2", {})
        assert not os.path.exists(counter), "the single history file came back"


class TestCorrectnessIsUnchanged:

    def test_a_restart_still_deduplicates_against_today(self, counter):
        oh.log_flight_count("UAL1", {"origin": "EWR"})
        oh._counter_mem.update(path=None, day=None, doc=None, seen=set())  # restart
        oh.log_flight_count("UAL1", {"origin": "EWR"})
        assert oh.load_counter_log()[TODAY]["count"] == 1

    def test_readers_see_the_same_shape_as_before(self, counter):
        oh.log_flight_count("UAL1", {"origin": "EWR", "destination": "LAX", "plane": "B738"})
        day = oh.load_counter_log()[TODAY]
        assert set(day) >= {"date", "count", "flights", "first_seen", "last_seen"}
        f = day["flights"][0]
        assert (f["callsign"], f["origin"], f["dest"], f["aircraft"]) == \
               ("UAL1", "EWR", "LAX", "B738")

    def test_a_new_day_starts_a_new_file(self, counter):
        oh.log_flight_count("UAL1", {})
        oh._counter_mem["day"] = "1999-01-01"      # as if yesterday's state were held
        oh.log_flight_count("UAL1", {})            # same callsign, new day file
        assert oh.load_counter_log()[TODAY]["count"] == 1


class TestMigrationFromTheSingleFile:

    def _legacy(self, path, days):
        log = {d: {"date": d, "count": n, "first_seen": "00:00:00",
                   "last_seen": "23:00:00",
                   "flights": [{"callsign": f"X{i}", "time": "01:00:00", "hour": 1,
                                "origin": "", "dest": "", "aircraft": ""}
                               for i in range(n)]}
               for d, n in days.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log, f)
        return log

    def test_every_day_survives_the_split(self, counter):
        before = self._legacy(counter, {_day(3): 4, _day(2): 7, _day(1): 2})
        oh.log_flight_count("NEW1", {})
        after = oh.load_counter_log()
        for d, doc in before.items():
            assert after[d] == doc, f"{d} changed in migration"
        assert after[TODAY]["count"] == 1

    def test_the_old_file_is_kept_aside_not_deleted(self, counter):
        self._legacy(counter, {_day(1): 3})
        oh.log_flight_count("NEW1", {})
        assert not os.path.exists(counter)
        assert os.path.exists(counter + ".migrated"), "history deleted, not kept"

    def test_todays_flights_from_the_old_file_still_deduplicate(self, counter):
        self._legacy(counter, {TODAY: 2})           # X0, X1 already counted today
        oh.log_flight_count("X0", {})
        assert oh.load_counter_log()[TODAY]["count"] == 2

    def test_an_interrupted_migration_resumes_without_overwriting(self, counter):
        self._legacy(counter, {_day(2): 3, _day(1): 5})
        d = oh._counter_dir()
        os.makedirs(d)
        done = {"date": _day(2), "count": 99, "flights": [], "first_seen": "", "last_seen": ""}
        with open(os.path.join(d, f"{_day(2)}.json"), "w") as f:
            json.dump(done, f)                     # written before the "crash"
        oh.log_flight_count("NEW1", {})
        log = oh.load_counter_log()
        assert log[_day(2)]["count"] == 99, "an already-split day was overwritten"
        assert log[_day(1)]["count"] == 5, "the unfinished day was not split"

    def test_readers_are_complete_before_migration_runs(self, counter):
        """The web server can read while the display has not migrated yet."""
        before = self._legacy(counter, {_day(1): 3})
        assert oh.load_counter_log() == before


class TestRetention:

    def test_days_past_retention_are_removed(self, counter):
        d = oh._counter_dir()
        os.makedirs(d)
        for n in (200, 100, 5):
            with open(os.path.join(d, f"{_day(n)}.json"), "w") as f:
                json.dump({"date": _day(n), "count": 0, "flights": []}, f)
        with patch.dict(sys.modules["config"].__dict__, {"STATS_LOG_DAYS": 90}):
            oh.log_flight_count("UAL1", {})
        days = set(oh.load_counter_log())
        assert _day(200) not in days and _day(100) not in days
        assert _day(5) in days and TODAY in days


class TestClosestAndFarthestAreNotReparsedEveryCycle:
    """Both lists are read for every flight on every cycle. On ernie the parse
    cost 34 + 54 ms with the GIL held, though only this process writes them."""

    @pytest.fixture
    def rec(self, tmp_path):
        oh._record_cache.clear()
        p = str(tmp_path / "close.txt")
        with open(p, "w") as f:
            json.dump([{"callsign": "A", "distance": 1.0}], f)
        yield p
        oh._record_cache.clear()

    def test_an_unchanged_file_is_parsed_once(self, rec):
        oh._load_record_list(rec)
        reads = [c for c in _opens(lambda: oh._load_record_list(rec)) if "w" not in c[1]]
        assert reads == [], f"re-parsed an unchanged file: {reads}"

    def test_our_own_write_does_not_force_a_reparse(self, rec):
        lst = oh._load_record_list(rec)
        lst.append({"callsign": "B", "distance": 2.0})
        oh._write_record_list(rec, lst)
        reads = [c for c in _opens(lambda: oh._load_record_list(rec)) if "w" not in c[1]]
        assert reads == []
        assert [e["callsign"] for e in oh._load_record_list(rec)] == ["A", "B"]

    def test_a_change_made_elsewhere_is_picked_up(self, rec):
        """A restore from backup rewrites the file behind our back."""
        oh._load_record_list(rec)
        with open(rec, "w") as f:
            json.dump([{"callsign": "RESTORED", "distance": 0.5},
                       {"callsign": "X", "distance": 9.0}], f)
        assert [e["callsign"] for e in oh._load_record_list(rec)] == ["RESTORED", "X"]

    def test_callers_mutating_what_they_got_does_not_corrupt_the_cache(self, rec):
        """log_flight_data appends and sorts the list it is handed, and may
        then return early without writing — the cache must not see that."""
        oh._load_record_list(rec)               # miss: populates the cache
        lst = oh._load_record_list(rec)         # HIT: this is the case at risk
        lst.append({"callsign": "UNSAVED", "distance": 0.1})
        lst.sort(key=lambda e: e["distance"])
        assert [e["callsign"] for e in oh._load_record_list(rec)] == ["A"]
