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
    oh._counter_mem.update(path=None, day=None, doc=None, seen=set(), migrated=None)
    yield path
    oh._counter_mem.update(path=None, day=None, doc=None, seen=set(), migrated=None)


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
        oh._counter_mem.update(path=None, day=None, doc=None, seen=set(), migrated=None)  # restart
        oh.log_flight_count("UAL1", {"origin": "EWR"})
        assert oh.load_counter_log()[TODAY]["count"] == 1

    def test_readers_see_the_same_shape_as_before(self, counter):
        oh.log_flight_count("UAL1", {"origin": "EWR", "destination": "LAX", "plane": "B738"})
        day = oh.load_counter_log()[TODAY]
        assert set(day) >= {"date", "count", "flights", "first_seen", "last_seen"}
        f = day["flights"][0]
        assert (f["callsign"], f["origin"], f["dest"], f["aircraft"]) == \
               ("UAL1", "EWR", "LAX", "B738")



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



class _Clock:
    """Stand-in for overhead.datetime whose now() can be moved."""

    def __init__(self, when):
        self.when = when

    def now(self):
        return self.when


class TestMidnight:
    """The old test for this set the in-memory day by hand and logged a flight
    already counted — it passed with the rollover code deleted. This one moves
    the clock across midnight with the same callsign either side."""

    def test_the_same_flight_counts_again_on_the_new_day(self, counter):
        clock = _Clock(datetime(2026, 10, 3, 23, 59, 59))
        with patch.object(oh, "datetime", clock):
            oh.log_flight_count("UAL1", {})
            oh.log_flight_count("DAL2", {})
            clock.when = datetime(2026, 10, 4, 0, 0, 0)
            oh.log_flight_count("UAL1", {})
        log = oh.load_counter_log()
        assert log["2026-10-03"]["count"] == 2
        assert log["2026-10-04"]["count"] == 1, "UAL1 was treated as seen on the new day"
        assert log["2026-10-04"]["date"] == "2026-10-04"
        assert log["2026-10-04"]["first_seen"] == "00:00:00"

    def test_yesterday_is_not_written_into_todays_file(self, counter):
        clock = _Clock(datetime(2026, 10, 3, 23, 59, 59))
        with patch.object(oh, "datetime", clock):
            oh.log_flight_count("UAL1", {})
            clock.when = datetime(2026, 10, 4, 0, 0, 1)
            oh.log_flight_count("BAW3", {})
        log = oh.load_counter_log()
        assert [f["callsign"] for f in log["2026-10-04"]["flights"]] == ["BAW3"]
        assert [f["callsign"] for f in log["2026-10-03"]["flights"]] == ["UAL1"]


class TestWhenAWriteFails:

    def test_a_flight_whose_write_failed_is_retried(self, counter):
        """Marking it seen before the write meant it was never retried, and a
        restart before the next new flight lost it for good."""
        with patch.object(oh, "safe_write_json", return_value=False):
            oh.log_flight_count("UAL1", {})
        oh.log_flight_count("UAL1", {})                 # writer healthy again
        assert oh.load_counter_log()[TODAY]["count"] == 1

    def test_a_failed_write_leaves_memory_unchanged(self, counter):
        oh.log_flight_count("UAL1", {})
        with patch.object(oh, "safe_write_json", return_value=False):
            oh.log_flight_count("DAL2", {})
        assert "DAL2" not in oh._counter_mem["seen"]
        assert oh._counter_mem["doc"]["count"] == 1

    def test_migration_keeps_the_old_file_if_a_day_could_not_be_written(self, counter):
        """The rename used to happen regardless, so a permissions problem left
        the history invisible (in .migrated) and counting stopped."""
        with open(counter, "w") as f:
            json.dump({_day(1): {"date": _day(1), "count": 1, "flights": []}}, f)
        with patch.object(oh, "safe_write_json", return_value=False):
            oh._migrate_legacy_counter()
        assert os.path.exists(counter), "history was retired though nothing was written"
        assert not os.path.exists(counter + ".migrated")
        assert oh.load_counter_log()[_day(1)]["count"] == 1

    def test_a_migration_that_raises_is_not_retried_on_every_flight(self, counter):
        """A retry per call would re-parse the whole legacy file per flight per
        cycle — the freeze this design exists to remove."""
        calls = []

        def boom():
            calls.append(1)
            raise OSError("read-only file system")
        with patch.object(oh, "_migrate_legacy_counter", boom):
            for cs in ("A1", "B2", "C3", "A1"):
                oh.log_flight_count(cs, {})
        assert len(calls) == 1, f"migration attempted {len(calls)} times"
        assert oh.load_counter_log()[TODAY]["count"] == 3

    def test_a_failing_migration_is_not_retried_at_every_midnight_either(self, counter):
        """Each attempt parses the whole legacy file; a persistent failure
        should cost that once per process, not once per day."""
        calls = []

        def boom():
            calls.append(1)
            raise OSError("read-only file system")
        clock = _Clock(datetime(2026, 10, 3, 23, 0, 0))
        with patch.object(oh, "_migrate_legacy_counter", boom), \
             patch.object(oh, "datetime", clock):
            oh.log_flight_count("A1", {})
            clock.when = datetime(2026, 10, 4, 0, 30, 0)
            oh.log_flight_count("A1", {})
            clock.when = datetime(2026, 10, 5, 0, 30, 0)
            oh.log_flight_count("A1", {})
        assert len(calls) == 1, f"migration attempted {len(calls)} times over 3 days"


class TestLegacyEdgeCases:

    def test_a_corrupt_legacy_file_is_set_aside_not_reparsed_forever(self, counter):
        with open(counter, "w") as f:
            f.write("{truncated by a power cut")
        oh.log_flight_count("UAL1", {})
        assert not os.path.exists(counter)
        assert os.path.exists(counter + ".corrupt"), "the damaged file was deleted"
        assert oh.load_counter_log()[TODAY]["count"] == 1

    def test_day_files_win_over_an_unmigrated_legacy_copy(self, counter):
        with open(counter, "w") as f:
            json.dump({_day(1): {"date": _day(1), "count": 1, "flights": []}}, f)
        d = oh._counter_dir()
        os.makedirs(d)
        with open(os.path.join(d, f"{_day(1)}.json"), "w") as f:
            json.dump({"date": _day(1), "count": 5, "flights": []}, f)
        assert oh.load_counter_log()[_day(1)]["count"] == 5


class TestRetentionEdges:

    @pytest.mark.parametrize("days", [0, -5, None])
    def test_no_positive_limit_means_keep_everything(self, counter, days):
        """A negative limit would put the cutoff in the FUTURE and delete every
        past day — the guard has to catch it, not only 0."""
        d = oh._counter_dir()
        os.makedirs(d)
        with open(os.path.join(d, f"{_day(400)}.json"), "w") as f:
            json.dump({"date": _day(400), "count": 0, "flights": []}, f)
        with patch.dict(sys.modules["config"].__dict__, {"STATS_LOG_DAYS": days}):
            oh.log_flight_count("UAL1", {})
        assert _day(400) in oh.load_counter_log(), f"{days!r} deleted history"

    def test_stale_temp_files_are_swept(self, counter):
        d = oh._counter_dir()
        os.makedirs(d)
        stale = os.path.join(d, f"{_day(1)}.json.tmp.4242")
        fresh = os.path.join(d, f"{TODAY}.json.tmp.4243")
        for p in (stale, fresh):
            with open(p, "w") as f:
                f.write("{}")
        old = __import__("time").time() - 7200
        os.utime(stale, (old, old))
        oh.log_flight_count("UAL1", {})
        assert not os.path.exists(stale), "an orphaned temp file was kept"
        assert os.path.exists(fresh), "a temp file possibly mid-write was removed"


class TestCachedEntriesAreNotTheCallersDicts:

    def test_mutating_an_entry_after_writing_does_not_change_the_cache(self, tmp_path):
        """log_farthest_flight adds keys to the same entry dict log_flight_data
        just cached; those keys must not appear in the cached closest list."""
        oh._record_cache.clear()
        p = str(tmp_path / "close.txt")
        entry = {"callsign": "UAL1", "distance": 1.0}
        oh._write_record_list(p, [entry])
        entry["_airport"] = "LHR"
        entry["reason"] = "origin"
        cached = oh._load_record_list(p)
        assert cached == [{"callsign": "UAL1", "distance": 1.0}], cached
        with open(p) as f:
            assert json.load(f) == cached, "cache and disk disagree"
        oh._record_cache.clear()

    def test_a_failed_write_is_not_cached_as_if_it_landed(self, tmp_path):
        oh._record_cache.clear()
        p = str(tmp_path / "close.txt")
        oh._write_record_list(p, [{"callsign": "A"}])
        with patch.object(oh, "safe_write_json", return_value=False):
            oh._write_record_list(p, [{"callsign": "NEVER_SAVED"}])
        assert oh._load_record_list(p) == [{"callsign": "A"}]
        oh._record_cache.clear()
