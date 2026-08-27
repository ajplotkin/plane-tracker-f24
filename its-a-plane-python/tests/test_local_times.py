"""
test_local_times.py — the departure/arrival time layer.

Written because an adversarial review planted 14 bugs in this layer and 13 of
them survived the entire suite: the arrival delay could go negative, prefer the
wrong revision, or fall through to the field we deliberately distrust; the
suppression rule could be INVERTED; the day marker could flip sign or vanish;
the arrival segment could be wired to the departure's timestamps. Every test
below names the mutation it exists to catch.
"""

import datetime
import os
import time

import pytest

from utilities.airlabs import arrival_delay, departure_delay, local_wall, revised_ts


def _sched(**over):
    """UA2017 SEA->EWR, 2026-08-21: 44 late off the gate, 22 late on arrival."""
    s = {
        "dep_time": "2026-08-20 22:58", "dep_time_ts": 1787291880,
        "dep_time_utc": "2026-08-21 05:58",
        "dep_estimated": "2026-08-20 23:42", "dep_estimated_ts": 1787294520,
        "dep_estimated_utc": "2026-08-21 06:42", "dep_delayed": 44,
        "arr_time": "2026-08-21 07:10", "arr_time_ts": 1787310600,
        "arr_time_utc": "2026-08-21 11:10",
        "arr_estimated": "2026-08-21 07:32", "arr_estimated_ts": 1787311920,
        "arr_estimated_utc": "2026-08-21 11:32", "arr_delayed": 22,
    }
    s.update(over)
    return s


# ---------------------------------------------------------------------------
# arrival_delay
# ---------------------------------------------------------------------------

def test_arrival_and_departure_delays_are_independent():
    """They genuinely diverge — this flight makes up half its delay in the air.
    Wiring the arrival to the departure figure overstates it two-fold."""
    assert departure_delay(_sched())[0] == 44
    assert arrival_delay(_sched())[0] == 22


def test_an_early_arrival_reports_zero_not_a_negative():
    """MUTATION: dropping max(delta, 0). A negative delay would render "+-12m"
    and colour a flight red for being EARLY."""
    early = _sched(arr_estimated="2026-08-21 06:58", arr_estimated_ts=1787309880,
                   arr_estimated_utc="2026-08-21 10:58", arr_delayed=-12)
    minutes, _ = arrival_delay(early)
    assert minutes == 0, f"early arrival reported {minutes}"


def test_actual_arrival_beats_estimated():
    """MUTATION: reversing the ("actual", "estimated") precedence. What HAPPENED
    outranks what was predicted."""
    both = _sched(arr_actual="2026-08-21 07:50", arr_actual_ts=1787313000,
                  arr_actual_utc="2026-08-21 11:50")
    minutes, revised = arrival_delay(both)
    assert (minutes, revised) == (40, "2026-08-21 07:50"), (minutes, revised)


def test_arrival_never_falls_through_to_the_ambiguous_delayed_field():
    """MUTATION: adding `delayed` to the fallback chain. It is not reliably
    either side — 100 EWR rows on 2026-08-17 had it equal to arr_delayed, while
    UA2017 on 2026-08-21 had it equal to dep_delayed. Only arr_delayed counts."""
    tempting = {"arr_time": "2026-08-21 07:10", "arr_time_ts": 1787310600,
                "delayed": 99}
    assert arrival_delay(tempting) == (None, ""), arrival_delay(tempting)


def test_arrival_delay_of_zero_is_distinct_from_unknown():
    assert arrival_delay(_sched(arr_delayed=0, arr_estimated="", arr_estimated_ts=None,
                                arr_estimated_utc=""))[0] == 0
    assert arrival_delay({"arr_time": "2026-08-21 07:10"})[0] is None


# ---------------------------------------------------------------------------
# revised_ts
# ---------------------------------------------------------------------------

def test_revised_ts_matches_the_revision_the_delay_used():
    """MUTATION: choosing the first *_ts present instead of the authoritative
    revision. With arr_actual as STRINGS ONLY plus an arr_estimated_ts, the
    delay came from actual (07:32) while the timestamp came from estimated
    (07:20) — the parenthesised local time contradicting the time it annotates."""
    lopsided = _sched(arr_actual="2026-08-21 07:32", arr_actual_utc="2026-08-21 11:32",
                      arr_estimated="2026-08-21 07:20", arr_estimated_ts=1787311200,
                      arr_estimated_utc="2026-08-21 11:20")
    minutes, revised = arrival_delay(lopsided)
    ts = revised_ts(lopsided, "arr", minutes)
    assert revised == "2026-08-21 07:32"
    assert abs(ts - 1787311920) < 60, f"ts is {ts}, expected the 07:32 instant"


def test_revised_ts_is_none_when_there_is_nothing_to_revise():
    assert revised_ts(_sched(), "arr", None) is None
    assert revised_ts(None, "arr", 22) is None


def test_revised_ts_falls_back_to_shifting_the_schedule():
    """No usable revision timestamp: derive the instant the same way the delay
    derived the wall time."""
    only_duration = {"arr_time": "2026-08-21 07:10", "arr_time_ts": 1787310600,
                     "arr_delayed": 22}
    assert revised_ts(only_duration, "arr", 22) == 1787310600 + 22 * 60


def test_revised_ts_tolerates_string_timestamps():
    s = _sched(arr_estimated_ts="1787311920")
    assert revised_ts(s, "arr", 22) == 1787311920.0


# ---------------------------------------------------------------------------
# local_wall — the suppression rule and the day marker
# ---------------------------------------------------------------------------

@pytest.fixture
def eastern():
    """Pin the zone: this layer resolves the PANEL's clock, and the fleet is
    America/New_York. Without pinning, these assertions test the host."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_a_different_zone_is_converted_and_marked(eastern):
    wall, tz, day = local_wall(1787294520, "2026-08-20 23:42")   # 23:42 SEA
    assert wall == "2026-08-21 02:42"
    assert tz == "EDT"
    assert day == "+1", "the departure crosses midnight into the panel's day"


def test_the_same_zone_is_suppressed(eastern):
    """MUTATION: inverting the comparison, so the local time shows only when it
    MATCHES. That renders a redundant duplicate on every same-zone time and
    hides every genuine conversion — and it survived the whole suite."""
    assert local_wall(1787311920, "2026-08-21 07:32") == (None, "", "")


def test_a_westward_flight_marks_the_day_backwards(eastern):
    """MUTATION: flipping the day-marker sign. Sydney -> the panel goes BACK a
    day; reporting +1 there is exactly wrong."""
    # 2026-08-22 09:00 in Sydney (UTC+10) is 2026-08-21 19:00 EDT.
    ts = datetime.datetime(2026, 8, 21, 23, 0, tzinfo=datetime.timezone.utc).timestamp()
    wall, _, day = local_wall(ts, "2026-08-22 09:00")
    assert wall.startswith("2026-08-21"), wall
    assert day == "-1", f"expected -1, got {day!r}"


def test_the_day_marker_is_empty_when_the_date_holds(eastern):
    """MUTATION: emitting a marker unconditionally. Most conversions do not
    cross midnight and "+0" is noise."""
    ts = datetime.datetime(2026, 8, 21, 20, 0, tzinfo=datetime.timezone.utc).timestamp()
    _, _, day = local_wall(ts, "2026-08-21 13:00")     # 13:00 PDT -> 16:00 EDT
    assert day == ""


def test_zones_exactly_24_hours_apart_are_not_mistaken_for_the_same_zone(eastern):
    """The comparison is on the full DATE and time, not just HH:MM. Kiritimati
    (UTC+14) and Honolulu (UTC-10) read the same clock a day apart, and an
    HH:MM-only rule hid both the conversion and the day marker that was the
    entire point of showing it."""
    # 2026-08-21 10:00 UTC+14 == 2026-08-20 10:00 UTC-10 == 2026-08-20 16:00 EDT
    ts = datetime.datetime(2026, 8, 20, 20, 0, tzinfo=datetime.timezone.utc).timestamp()
    wall, _, day = local_wall(ts, "2026-08-21 10:00")
    assert wall is not None, "a 24h-apart zone was suppressed as if it were local"
    assert day == "-1"


def test_the_offset_is_resolved_at_the_flight_s_instant_not_now(eastern):
    """A November arrival is EST even when computed in August. The mirror used
    to derive this from a single offset captured at request time, which
    fabricated a conversion on a same-zone arrival across the DST boundary."""
    ts = datetime.datetime(2026, 11, 1, 10, 30, tzinfo=datetime.timezone.utc).timestamp()
    wall, tz, _ = local_wall(ts, "2026-11-01 02:30")   # 02:30 CST -> 05:30 EST
    assert tz == "EST", f"got {tz}"
    assert wall == "2026-11-01 05:30", wall


def test_malformed_input_degrades_rather_than_raising(eastern):
    for ts, ticket in ((None, "2026-08-21 07:10"), ("", "2026-08-21 07:10"),
                       (1787311920, ""), ("abc", "2026-08-21 07:10"),
                       (1787311920, "not-a-time"), (1e30, "2026-08-21 07:10")):
        assert local_wall(ts, ticket) == (None, "", ""), (ts, ticket)


# ---------------------------------------------------------------------------
# the scene wiring — each end must read its OWN fields
# ---------------------------------------------------------------------------

def _line(**over):
    from scenes.trackedstats import _build_stats
    d = {"is_scheduled": True, "origin": "SEA", "destination": "EWR",
         "dep_time": "2026-08-20 22:58", "arr_time": "2026-08-21 07:10"}
    d.update(over)
    return "".join(ch for ch, _ in _build_stats(d))


def test_the_arrival_segment_reads_arrival_fields_not_departure_ones():
    """MUTATION: wiring the arrival segment to the dep_* locals — a plausible
    copy-paste that survived the whole suite. Here only the DEPARTURE converts,
    so exactly one parenthesised time may appear, and it must sit on Dep."""
    line = _line(dep_time_local="2026-08-21 01:58", dep_time_local_tz="EDT",
                 dep_time_local_day="+1",
                 arr_time_local=None, arr_time_local_tz="", arr_time_local_day="")
    assert line.count("(") == 1, line
    dep_half, arr_half = line.split("Arr ")
    assert "(" in dep_half and "(" not in arr_half, line


def test_the_departure_segment_reads_departure_fields_not_arrival_ones():
    """The mirror-image case: only the ARRIVAL converts, as on any flight
    leaving the panel's own zone for another."""
    line = _line(dep_time_local=None, dep_time_local_tz="", dep_time_local_day="",
                 arr_time_local="2026-08-21 04:10", arr_time_local_tz="EDT",
                 arr_time_local_day="")
    assert line.count("(") == 1, line
    dep_half, arr_half = line.split("Arr ")
    assert "(" not in dep_half and "(" in arr_half, line


def test_a_suppressed_local_renders_nothing_at_all():
    line = _line(dep_time_local=None, arr_time_local=None)
    assert "(" not in line and "EDT" not in line, line


def test_the_delayed_variant_of_the_local_is_used_when_the_delay_shows():
    """The line shows the REVISED ticket time when delayed, so it must show the
    REVISED local beside it — not the scheduled one."""
    line = _line(dep_delay_min=44, dep_time_revised="2026-08-20 23:42",
                 dep_time_local="2026-08-21 01:58", dep_time_local_tz="EDT",
                 dep_time_local_day="+1",
                 dep_time_revised_local="2026-08-21 02:42",
                 dep_time_revised_local_tz="EDT", dep_time_revised_local_day="+1")
    assert "2:42" in line, line
    assert "1:58" not in line, f"showed the scheduled local against the revised time: {line}"


# ---------------------------------------------------------------------------
# the kern reaches the rendered line
# ---------------------------------------------------------------------------

# NOTE: whether the SCENES actually apply the kern is pinned in the e2e harness
# (testing/e2e_debug.py, scenario "tracked"), which compares the width the draw
# loop accumulated against an independently computed one. A unit test here could
# only reimplement the loop and would pass against a broken call site.


def test_multi_character_segments_are_kerned_per_glyph():
    """MUTATION: looking the segment up whole. scenes/trackedroute.py appends
    " -> " as ONE three-character segment, so a per-segment lookup returned 0
    and left two wide gaps on the very line the kern was meant to harmonise."""
    from setup.fonts import kern_5x8
    seg = " → "
    assert kern_5x8(seg) == 0, "a whole segment is not in the table, by design"
    assert sum(kern_5x8(c) for c in seg) == -4, "and per-glyph is what the scenes must use"
