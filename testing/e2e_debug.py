"""End-to-end headless debug of the its-a-plane display pipeline.

Runs the REAL display module (all scenes, real animator, real fonts) against
a faithful rgbmatrix stub, drives synthetic multi-flight data through the
whole loop, and checks the flicker mechanism at frame granularity:
writes into the page-indicator zone must occur ONLY when the page changes.

Usage: python e2e_debug.py <workdir> <scenario>
  scenario: multi | single | reset | iss
"""
# The tracked scenario asserts on panel-local times, and utilities.airlabs
# resolves them with datetime.fromtimestamp(), which reads the HOST's zone. The
# fleet is America/New_York; without pinning it here the scenario passes only on
# an Eastern-time machine and fails under TZ=UTC for reasons that have nothing
# to do with the code under test.
import os as _os_tz
_os_tz.environ["TZ"] = "America/New_York"
try:
    import time as _time_tz
    _time_tz.tzset()
except AttributeError:          # not POSIX
    pass

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "work")
SCENARIO = sys.argv[2] if len(sys.argv) > 2 else "multi"

sys.path.insert(0, HERE)
sys.path.insert(0, WORK)
os.chdir(WORK)  # config.py, logos/, .cache/ are CWD-relative
os.makedirs(".cache", exist_ok=True)

# Fail fast on a broken work copy: its-a-plane-python/logos is a symlink to
# ../logo, so a plain `cp -R` copies a dangling link and the reset scenario's
# logo-repaint assertion fails misleadingly. Use `cp -RL` to build work copies.
_logos = os.path.join(WORK, "logos")
if not os.path.isdir(_logos) or not os.listdir(_logos):
    sys.exit(f"SETUP ERROR: {_logos} is missing, empty, or a dangling symlink.\n"
             "Build the work copy with `cp -RL its-a-plane-python/ <workdir>/` "
             "so the logos/ symlink is dereferenced.")

import fake_rgbmatrix
fake_rgbmatrix.install()
RECORDER = fake_rgbmatrix.RECORDER

# ---- virtual clock: wall time advances with the frame counter ----------------
# Some of what the app draws is driven by the REAL wall clock, most visibly the
# ISS badge blink in scenes/flightdetails.py: its phase is int(time.time()//2)%2,
# deliberately wall-clock-derived so the browser mirror (which blinks on
# floor(serverNow()/2)%2) stays in lockstep instead of drifting to anti-phase.
# This harness replays frames back-to-back with no sleeping, so a whole
# 1500-frame scenario takes well under a second of real time: every frame lands
# inside the SAME 2 s blink phase, and any assertion about the blink passes or
# fails purely on what time of day the run started (isscameo's badge check
# alternated pass/fail across back-to-back runs).
#
# So the harness supplies the wall time the replay skips: the clock the app
# reads advances one frame period per replayed frame, exactly as it does on the
# device. 1500 frames == 150 virtual seconds, so every wall-clock cycle shorter
# than the scenario is exercised — both blink faces, always. The base is floored
# to a multiple of FOUR seconds so frame 0 starts in phase 0, making runs
# reproducible rather than merely non-flaky, and it tracks the real date so cache
# TTLs and datetime.now() (not patched — it does not read time.time()) stay
# consistent.
#
# Four, not two. The phase is int(t//2)%2, so phase 0 needs floor(t/2) to be
# EVEN, i.e. t in [4k, 4k+2). Flooring to an even second only aligns the
# boundary; the parity still flips every 2 s of start time, so "frame 0 is phase
# 0" held on exactly half of runs. Nothing caught it because no assertion
# depended on WHICH face was up — but issfull dumps canvas snapshots for
# pixel-diffing two code versions, and the indicator zone came out inverted
# between runs of identical code roughly half the time.
import time as _time_module  # noqa: E402
from setup import frames as _frames  # noqa: E402 (frame rate comes from setup/frames.py)

VCLOCK = {"frame": 0}  # run_frames() keeps this in step with Animator.frame
_VCLOCK_BASE = (int(_time_module.time()) // 4) * 4.0


def virtual_time():
    """time.time() as the app sees it: frame N is base + N * PERIOD."""
    return _VCLOCK_BASE + VCLOCK["frame"] * _frames.PERIOD


_time_module.time = virtual_time

# ---- stub network-facing modules BEFORE importing display -------------------
import types


class StubOverhead:
    def __init__(self):
        self._flights = []
        self._new_data = False
        self.grab_calls = 0
        self.iss_pass_data = None
        self.tracked_data = None

    def inject(self, flights):
        self._flights = flights
        self._new_data = True

    @property
    def new_data(self):
        return self._new_data

    @property
    def data(self):
        self._new_data = False  # real Overhead.data clears the flag on read
        return self._flights

    @property
    def data_is_empty(self):
        return len(self._flights) == 0

    @property
    def processing(self):
        return False

    def grab_data(self):
        self.grab_calls += 1


ov_mod = types.ModuleType("utilities.overhead")
ov_mod.Overhead = StubOverhead
sys.modules["utilities.overhead"] = ov_mod

# Weather stubs are configurable per scenario (values readable at call time)
STUB_WEATHER = {"forecast": None, "temp": None, "hum": None}
tmp_mod = types.ModuleType("utilities.temperature")
tmp_mod.grab_forecast = lambda *a, **k: STUB_WEATHER["forecast"]
tmp_mod.grab_temperature_and_humidity = lambda *a, **k: (STUB_WEATHER["temp"], STUB_WEATHER["hum"])
sys.modules["utilities.temperature"] = tmp_mod

iss_mod = types.ModuleType("utilities.iss")
iss_mod.is_iss_visible_now = lambda *a, **k: False
sys.modules["utilities.iss"] = iss_mod

# Network-free stubs for the alert/tide utilities the idle scenes poll
for name, attrs in {
    "utilities.rain": {"get_rain_alert": lambda: None, "get_wind_info": lambda: None},
    "utilities.nws_alerts": {"get_active_alerts": lambda: []},
    "utilities.airport_status": {"get_airport_alerts": lambda: []},
    "utilities.tides": {"get_next_tides": lambda: None,
                        "get_water_temp": lambda: None,
                        "is_water_temp_fallback": lambda: False},
}.items():
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m

# ---- import the real app -----------------------------------------------------
from display import Display  # noqa: E402
from rgbmatrix import graphics  # noqa: E402 (the fake)

# ---- synthetic flights --------------------------------------------------------
def flight(cs, fn, airline, icao, direction):
    return {
        "callsign": cs, "flight_number": fn, "airline": airline,
        "owner_icao": icao, "direction": direction,
        "origin": "EWR", "destination": "SFO",
        "distance_origin": 12.0, "distance_destination": 2100.0,
        "time_scheduled_departure": 1751380000, "time_real_departure": 1751380600,
        "time_scheduled_arrival": 1751402000, "time_estimated_arrival": 1751402300,
        "plane": "B739", "distance": 3.2, "altitude": 36000,
        "vertical_speed": 0, "heading": 45,
    }


FLIGHTS4 = [
    flight("UAL1234", "UA1234", "United Airlines", "UAL", 45),
    flight("DAL88", "DL88", "Delta Air Lines", "DAL", 130),
    # descenders g/j/y exercise legitimate row-24 writes by the flight line
    flight("CJT501", "W8501", "Cargojet Airways", "CJT", 220),
    flight("AAL9", "AA9", "American Airlines", "AAL", 300),
]

# ---- zones -------------------------------------------------------------------
IND_X0, IND_X1 = 52, 64
IND_Y0, IND_Y1 = 16, 24        # write-tracking incl. the y=16 boundary row
GLYPH_Y0 = 17                  # 5x8 glyphs at baseline 24 occupy rows 17-24

errors = {}       # scene method name -> first traceback
failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def expected_indicator_pixels(idx, total):
    c = fake_rgbmatrix.FrameCanvas()
    font = graphics.Font()
    font.LoadFont("fonts/4x6.bdf")
    graphics.DrawText(c, font, 52, 24, graphics.Color(192, 192, 192), f"{idx + 1}/{total}")
    return c.snapshot(IND_X0, IND_X1, IND_Y0, IND_Y1)


def run_frames(d, n, hook=None):
    """Replicates Animator.play() exactly, bounded, no sleep."""
    for _ in range(n):
        RECORDER.frame = d.frame
        VCLOCK["frame"] = d.frame  # virtual wall clock tracks the frame counter
        if hook:
            hook(d)
        for keyframe in d.keyframes:
            props = keyframe.properties
            RECORDER.context = keyframe.__name__
            try:
                if d.frame == 0 and props["divisor"] == 0:
                    keyframe()
                if (d.frame > 0 and props["divisor"]
                        and not ((d.frame - props["offset"]) % props["divisor"])):
                    if keyframe(props["count"]):
                        props["count"] = 0
                    else:
                        props["count"] += 1
            except Exception:
                key = keyframe.__name__
                if key not in errors:
                    errors[key] = traceback.format_exc()
        d.frame += 1


if SCENARIO == "idle":
    # Synthetic weather so the idle scenes actually render
    from datetime import datetime as _dt, timedelta as _td
    STUB_WEATHER["temp"], STUB_WEATHER["hum"] = 72.4, 55.0
    STUB_WEATHER["forecast"] = [
        {"startTime": (_dt.now().astimezone() + _td(days=i)).replace(
            hour=6, minute=0, second=0, microsecond=0).isoformat(),
         "values": {"weatherCodeFullDay": code, "temperatureMin": 60 + i,
                    "temperatureMax": 80 + i, "moonPhase": 3}}
        for i, code in enumerate(["1000", "1100", "1001"])
    ]

print(f"=== scenario: {SCENARIO} (workdir: {WORK}) ===")
d = Display()

if SCENARIO in ("multi", "old"):
    # -- main multi-flight run: 1200 frames (~2 minutes of display time) -------
    page_log = []      # (frame, index) whenever page state changes
    zone_frames = {}   # frame -> n writes in indicator zone
    content_bad = []
    prev_state = None

    def hook(d):
        global prev_state
        if d.frame == 30:
            d.overhead.inject(FLIGHTS4)

    run_frames(d, 1200, hook)

    # data becomes active at the first check_for_loaded_data keyframe, which
    # runs every 5 s. Was a literal 50 — right only at 10 fps, and at 15 fps it
    # started checking 25 frames before the data was even live, passing only
    # because nothing happened to be written in that window.
    DATA_ACTIVE = int(_frames.PER_SECOND * 5)
    reset_frames = set(RECORDER.clears)
    for f in range(DATA_ACTIVE, 1200):
        w = RECORDER.zone_writes(f, IND_X0, IND_X1, IND_Y0, IND_Y1)
        if w:
            zone_frames[f] = w

    first_paint = min(zone_frames) if zone_frames else None
    check("indicator painted after data arrival", first_paint is not None)

    # every frame with zone writes must coincide with a canvas Clear (scene
    # reset/page change) or be the first paint
    unexplained = {f: w for f, w in zone_frames.items()
                   if f != first_paint and f not in reset_frames
                   and (f - 1) not in reset_frames}
    by_writer = {}
    for f, w in unexplained.items():
        for (x, y, c, ctx) in w:
            by_writer.setdefault(ctx, []).append((f, x, y, c))
    check("zone writes only on page-change/reset frames", not unexplained,
          f"{len(unexplained)} frames; writers: " +
          "; ".join(f"{k}: {len(v)} writes e.g. {v[:3]}" for k, v in by_writer.items()))

    window = [f for f in range(DATA_ACTIVE, 1200)]
    stable = [f for f in window if f not in zone_frames]
    check("zone untouched on all non-page-change frames",
          len(window) - len(stable) <= len([f for f in reset_frames if f >= DATA_ACTIVE]) + 1,
          f"touched={len(window) - len(stable)}, resets={len(reset_frames)}")

    n_resets = len([f for f in reset_frames if f > 50])
    check("multiple page advances occurred (scroll cycled)", n_resets >= 3,
          f"resets={n_resets}")

    idx, total = d._data_index, len(d._data)
    got = d.canvas.snapshot(IND_X0, IND_X1, IND_Y0, IND_Y1)
    want = expected_indicator_pixels(idx, total)
    residue = {k: v for k, v in got.items() if k not in want}
    check(f"final zone content == '{idx + 1}/{total}' glyphs", got == want,
          f"got {len(got)} px, want {len(want)} px; residue {list(residue.items())[:6]}")

    # boundary-row ownership, by writer
    row16 = {}
    row24 = {}
    plane_above_25 = {}
    for f in range(DATA_ACTIVE, 1200):
        for (x, y, c, ctx) in RECORDER.writes.get(f, []):
            if y == 16:
                row16[ctx] = row16.get(ctx, 0) + 1
            if y == 24 and c != (0, 0, 0):
                row24[ctx] = row24.get(ctx, 0) + 1
            if ctx == "plane_details" and y < 25:
                plane_above_25[(x, y, c)] = f
    check("flight scene never writes row 16 (journey's distance row)",
          "flight_details" not in row16, f"writers: {row16}")
    check("plane line never writes above row 25 ('@' bleed clipped)",
          not plane_above_25, f"e.g. {list(plane_above_25.items())[:3]}")
    check("row-24 non-black writes come from the flight line only",
          set(row24) <= {"flight_details"}, f"writers: {row24}")
    print(f"INFO  row-16 writers: {row16}")
    print(f"INFO  row-24 non-black writers: {row24}")

elif SCENARIO == "single":
    def hook(d):
        if d.frame == 30:
            d.overhead.inject(FLIGHTS4[:1])
    run_frames(d, 400, hook)
    zone_writes_all = [f for f in range(0, 400)
                       if RECORDER.zone_writes(f, IND_X0, IND_X1, GLYPH_Y0, IND_Y1)]
    # single flight: NO indicator; scroll text legitimately crosses x>=52
    got = d.canvas.snapshot(IND_X0, IND_X1, GLYPH_Y0, IND_Y1)
    check("single-flight: scroll text does reach x>=52 (full-width path)",
          len(zone_writes_all) > 100, f"frames with writes: {len(zone_writes_all)}")
    idx_pix = expected_indicator_pixels(0, 1)
    check("single-flight: no '1/1' indicator drawn", not any(
        d.canvas.pixels.get(p) == (192, 192, 192) for p in idx_pix), "")

elif SCENARIO == "reset":
    # data-change reset with UNCHANGED page tuple (index 0, same count):
    # the reset hook must force an indicator repaint after clear_screen
    def hook(d):
        if d.frame == 30:
            d.overhead.inject(FLIGHTS4)
        if d.frame == 300:
            d.reset_scene()  # simulates check_for_loaded_data reset path
    run_frames(d, 400, hook)
    w300 = RECORDER.zone_writes(300, IND_X0, IND_X1, IND_Y0, IND_Y1)
    w301 = RECORDER.zone_writes(301, IND_X0, IND_X1, IND_Y0, IND_Y1)
    check("indicator repainted on first frame after manual reset_scene()",
          bool(w300 or w301), "no repaint within 1 frame")
    idx, total = d._data_index, len(d._data)
    got = d.canvas.snapshot(IND_X0, IND_X1, IND_Y0, IND_Y1)
    check("zone content correct after reset", got == expected_indicator_pixels(idx, total))
    # logo must be repainted after a same-flight reset (clear_screen wipes
    # the canvas; the old _logo_drawn flag skipped the redraw -> blank logo)
    logo_px = d.canvas.snapshot(0, 16, 0, 15)
    check("logo repainted after same-flight reset", len(logo_px) > 20,
          f"only {len(logo_px)} non-black px in logo area")

elif SCENARIO == "iss":
    def hook(d):
        if d.frame == 30:
            d.overhead.inject(FLIGHTS4)
        # hold the takeover flag for the window; the real isspass keyframe
        # clears it every frame while iss_pass_data is None
        if 200 <= d.frame < 220:
            d._iss_active = True
    run_frames(d, 300, hook)
    during = [f for f in range(201, 220)
              if RECORDER.zone_writes(f, IND_X0, IND_X1, IND_Y0, IND_Y1)]
    check("flight scene silent during ISS takeover", not during, f"wrote at {during[:5]}")
    w = [f for f in range(220, 240)
         if RECORDER.zone_writes(f, IND_X0, IND_X1, IND_Y0, IND_Y1)]
    check("indicator repainted after ISS takeover ends", bool(w), "no repaint in frames 220-240")

elif SCENARIO == "issfull":
    # Full ISS takeover: synthetic pass frames 100-249, snapshot the canvas
    # at the end of every takeover frame. Used to prove pixel-equivalence
    # between the old full-redraw isspass and the new incremental one, and
    # (new code only) that the canvas is NOT cleared per frame.
    import json
    OUT = sys.argv[3] if len(sys.argv) > 3 else None

    def make_iss(frame):
        if 100 <= frame < 250:
            p = (frame - 100) / 150.0
            return {"is_active": True, "progress": p,
                    "time_remaining_sec": int((1 - p) * 150),
                    "rise_compass": "NW", "set_compass": "SE",
                    "max_elevation": 78}
        return None

    snaps = {}

    def hook(d):
        # canvas state at start of frame f == end-of-frame state of f-1
        if 101 <= d.frame <= 250:
            snaps[d.frame - 1] = sorted(
                (x, y, c) for (x, y), c in d.canvas.pixels.items()
                if c != (0, 0, 0))
        d.overhead.iss_pass_data = make_iss(d.frame)

    run_frames(d, 300, hook)

    if OUT:
        with open(OUT, "w") as f:
            json.dump({str(k): v for k, v in snaps.items()}, f)
        print(f"INFO  wrote {len(snaps)} snapshots to {OUT}")

    mid_clears = [f for f in RECORDER.clears if 101 <= f <= 248]
    is_new_code = "iss_render" in open("scenes/isspass.py").read()
    if is_new_code:
        check("no per-frame canvas Clear during takeover", not mid_clears,
              f"clears at {mid_clears[:10]}")
        writes_per_frame = [len(RECORDER.writes.get(f, [])) for f in range(105, 245)]
        avg = sum(writes_per_frame) / len(writes_per_frame)
        print(f"INFO  avg pixel writes/frame during takeover: {avg:.0f}")
        check("takeover ends with scene reset (draw-once scenes recover)",
              any(f >= 250 for f in RECORDER.clears), str(RECORDER.clears[-3:]))

elif SCENARIO == "tracked":
    # Tracked-flight page: no zone flights, so _data stays empty and the two
    # tracked lines own the screen. Both must scroll on ONE shared position and
    # wrap TOGETHER on the wider line. They used to own a position each and wrap
    # at their own widths, so the shorter line lapped the longer one and the two
    # visibly started and stopped at different moments.
    # Real AirLabs figures for UA2017 on 2026-08-21: leaves SEA 44 late but makes
    # up time, arriving EWR only 22 late. Deliberately a flight whose DEPARTURE
    # is in another zone and whose ARRIVAL is in the panel's own, so the line
    # exercises both the converted and the suppressed case at once.
    TRACKED = {"callsign": "UAL2017", "number": "UAL2017",
               "airline_name": "United", "origin": "SEA", "destination": "EWR",
               "is_scheduled": True,
               "dep_time": "2026-08-20 22:58", "dep_time_ts": 1787291880,
               "dep_time_revised": "2026-08-20 23:42",
               "dep_time_revised_ts": 1787294520, "dep_delay_min": 44,
               # As utilities.airlabs.local_wall would resolve them: the SEA
               # departure converts (and crosses midnight here), the EWR arrival
               # does not, because EWR is already on the panel's clock.
               "dep_time_revised_local": "2026-08-21 02:42",
               "dep_time_revised_local_tz": "EDT",
               "dep_time_revised_local_day": "+1",
               "arr_time": "2026-08-21 07:10", "arr_time_ts": 1787310600,
               "arr_time_revised": "2026-08-21 07:32",
               "arr_time_revised_ts": 1787311920, "arr_delay_min": 22,
               "arr_time_revised_local": None,
               "arr_time_revised_local_tz": "",
               "arr_time_revised_local_day": ""}

    def hook(d):
        d.overhead.tracked_data = TRACKED

    run_frames(d, 900, hook)

    ROUTE_Y0, ROUTE_Y1 = 19 - 8 + 1, 19      # LINE1_Y - LOGO_SIZE + 1 .. LINE1_Y
    STATS_Y0, STATS_Y1 = 31 - 6, 31          # LINE3_Y - 6 .. LINE3_Y

    def leftmost(frame, y0, y1):
        """Smallest x of a non-black write in a row band, or None."""
        xs = [w[0] for w in RECORDER.zone_writes(frame, 0, 64, y0, y1)
              if w[2] != (0, 0, 0)]
        return min(xs) if xs else None

    route, stats = {}, {}
    for f in range(60, 900):
        r = leftmost(f, ROUTE_Y0, ROUTE_Y1)
        t_ = leftmost(f, STATS_Y0, STATS_Y1)
        if r is not None:
            route[f] = r
        if t_ is not None:
            stats[f] = t_
    # Not "every frame": the lines share one position and wrap on the WIDEST, so
    # the shorter line scrolls off and waits blank until the longer one finishes
    # — the same trade the main page makes. What matters is that both draw for a
    # substantial stretch and neither is permanently absent.
    check("both tracked lines draw", len(route) > 300 and len(stats) > 300,
          f"route {len(route)} frames, stats {len(stats)} frames")
    print(f"INFO  line-1 on screen {100*len(route)//max(len(stats),1)}% as long as line 3 "
          f"(route {len(route)}f, stats {len(stats)}f) — the shorter line waits")

    # A wrap shows up as the leftmost x jumping back toward the right edge.
    def wraps(series):
        ks = sorted(series)
        return [b for a, b in zip(ks, ks[1:]) if series[b] - series[a] > 20]

    rw, sw = wraps(route), wraps(stats)
    check("tracked lines wrap on the SAME frames (one shared position)",
          rw == sw and len(rw) >= 1, f"route wraps {rw[:6]}, stats wraps {sw[:6]}")

    # Only one position may exist: a scene keeping its own would drift again.
    check("no per-scene tracked scroll position survives",
          not hasattr(d, "_tr_pos") and not hasattr(d, "_ts_pos"),
          f"_tr_pos={hasattr(d, '_tr_pos')} _ts_pos={hasattr(d, '_ts_pos')}")
    check("both lines report a width to the shared driver",
          set(d._tracked_widths) == {"tracked_route", "tracked_stats"},
          f"reported: {sorted(d._tracked_widths)}")

    # The cycle must be the WIDER line's, not each line's own.
    if rw and len(rw) >= 2:
        cycle = rw[1] - rw[0]
        expected = 64 + max(d._tracked_widths.values()) + 1
        # Exact, not approximate: the cycle is fully determined (WIDTH + max
        # width + 1), and a +-2 tolerance let an off-by-one wrap condition
        # (`< 0` -> `<= 0`) through.
        check("cycle length follows the widest line",
              cycle == expected, f"cycle {cycle}px vs expected {expected}px")

    # The route is drawn once, on line 1 by trackedroute.py — not repeated in
    # the stats line. (journey.py is the ZONE-flight route line and returns when
    # len(self._data) == 0, so it never draws on this page at all.)
    from scenes.trackedstats import _build_stats
    stats_text = "".join(ch for ch, _ in _build_stats(TRACKED))
    # The codes were dropped from this line entirely (line 1 already shows the
    # route), so neither the codes nor the arrow-joined pair may appear here.
    check("stats line does not repeat the origin-destination pair",
          "SEA\u2192EWR" not in stats_text and "SEA \u2192 EWR" not in stats_text,
          repr(stats_text))
    check("airport codes stay on line 1, not repeated in the stats line",
          "SEA" not in stats_text and "EWR" not in stats_text, repr(stats_text))
    check("the panel-local conversion is present and labelled",
          "EDT" in stats_text or "EST" in stats_text, repr(stats_text))

    # The width the draw loop ACCUMULATED, against one computed independently.
    # Dropping fonts.kern_5x8 at either call site, or flipping its sign, passed
    # every unit test — the only kern tests called the table directly, so the
    # call sites themselves were unpinned. This is the check that catches it,
    # and it also guards the width that drives the shared scroll and the epoch.
    from setup.fonts import kern_5x8
    want_stats = sum(5 + kern_5x8(c) for c in stats_text)
    check("the stats line's reported width includes the kern",
          d._tracked_widths.get("tracked_stats") == want_stats,
          f"reported {d._tracked_widths.get('tracked_stats')}, "
          f"expected {want_stats} (unkerned would be {len(stats_text) * 5})")

    # Same for line 1, whose text is deterministic from the fixture. Its width
    # also carries the logo and the gap before the text starts.
    route_text = "United 2017 SEA \u2192 EWR"
    want_route = 8 + 2 + sum(5 + kern_5x8(c) for c in route_text)   # LOGO_SIZE + LOGO_GAP
    check("the route line's reported width includes the kern",
          d._tracked_widths.get("tracked_route") == want_route,
          f"reported {d._tracked_widths.get('tracked_route')}, expected {want_route}")

    # ---- a NEW tracked flight must RESTART the shared scroll --------------
    # Deleting the reset block from advance_tracked_scroll passed all 598 unit
    # tests and all nine scenarios: nothing anywhere swapped the tracked flight.
    # Without it the new flight inherits the old one's mid-scroll position and
    # starts part-way through.
    TRACKED2 = dict(TRACKED, callsign="DAL0009", number="DAL0009",
                    airline_name="Delta", origin="BOS", destination="LAX")
    # Park the scroll SAFELY mid-cycle first, then swap the flight. "ends at 64"
    # is satisfiable by accident in two different ways, and the first two
    # versions of this check hit both: a plain decrement from 65 lands on 64,
    # and starting one frame before the natural wrap (pos == -max_width) also
    # lands on 64 without any reset happening. The window below is past the
    # screen edge but far from the wrap point, so ONLY a reset can produce 64.
    wrap_at = -max(d._tracked_widths.values())        # pos where the cycle ends
    lo, hi = wrap_at + 40, -20                        # comfortably inside
    guard = 0
    while not (lo <= d._tracked_scroll_pos <= hi) and guard < 600:
        run_frames(d, 1, lambda dd: setattr(dd.overhead, "tracked_data", TRACKED))
        guard += 1
    pos_before = d._tracked_scroll_pos
    run_frames(d, 1, lambda dd: setattr(dd.overhead, "tracked_data", TRACKED2))
    check("a new tracked flight restarts the shared scroll",
          lo <= pos_before <= hi and d._tracked_scroll_pos == 64,
          f"pos {pos_before} -> {d._tracked_scroll_pos} "
          f"(parked in [{lo},{hi}]; only a reset can reach 64 from there)")

    # ---- ISS takeover must FREEZE the tracked scroll ----------------------
    # Removing the _iss_active guard also passed everything. The lines are not
    # drawn during a takeover, so advancing churns the position invisibly and
    # can fire a wrap (and an epoch write) against a clock nobody is watching.
    run_frames(d, 30, lambda dd: setattr(dd.overhead, "tracked_data", TRACKED2))
    ISS_ON = {"is_active": True, "progress": 0.5, "time_remaining_sec": 60,
              "rise_compass": "NW", "set_compass": "SE", "max_elevation": 70}

    def iss_hook(dd):
        dd.overhead.tracked_data = TRACKED2
        dd.overhead.iss_pass_data = ISS_ON

    run_frames(d, 5, iss_hook)          # let the takeover engage
    frozen_at = d._tracked_scroll_pos
    run_frames(d, 40, iss_hook)
    check("ISS takeover freezes the tracked scroll",
          d._tracked_scroll_pos == frozen_at,
          f"advanced {frozen_at} -> {d._tracked_scroll_pos} during takeover")

    # ---- the wrap must write the epoch the mirror reads -------------------
    # Removing the _write_tracked_epoch call passed everything too: the whole
    # mirror-sync path was untested, which is the same blind spot that let the
    # stale-filename bug in overhead.py survive.
    # NB: the harness chdir()s into WORK, which is the REAL app tree when one is
    # passed on the command line. So this must not delete anything there — it
    # notes the virtual time first and asserts the epoch was written DURING this
    # run, which is a sharper check than "the file exists" anyway.
    import json as _json
    epoch_file = os.path.join(WORK, ".cache", "tracked_scroll_epoch.json")
    written_after = virtual_time()

    def clear_iss(dd):
        dd.overhead.tracked_data = TRACKED2
        dd.overhead.iss_pass_data = None

    run_frames(d, 400, clear_iss)       # comfortably more than one cycle
    if not os.path.isfile(epoch_file):
        check("wrap writes the shared scroll epoch for the mirror", False,
              "tracked_scroll_epoch.json was never written")
    else:
        with open(epoch_file) as _f:
            ep = _json.load(_f)
        check("wrap writes the shared scroll epoch for the mirror",
              isinstance(ep.get("ts"), (int, float))
              and ep.get("width", 0) > 0
              and ep["ts"] >= written_after,
              f"{ep!r} (must be written after {written_after})")
        check("epoch width is the WIDEST line, not one of them",
              ep.get("width") == max(d._tracked_widths.values()),
              f"epoch {ep.get('width')} vs widths {d._tracked_widths}")

elif SCENARIO == "isscameo":
    # Continuous plane traffic through a long ISS pass: verify the cameo
    # (one flight scroll cycle) runs, then the takeover holds for the rest
    # of the pass, including across a mid-pass flight-list change.
    PASS_START, PASS_END = 200, 1400

    def make_iss(frame):
        if PASS_START <= frame < PASS_END:
            p = (frame - PASS_START) / float(PASS_END - PASS_START)
            return {"is_active": True, "progress": p,
                    "time_remaining_sec": int((1 - p) * 120),
                    "rise_compass": "NW", "set_compass": "SE",
                    "max_elevation": 78}
        return None

    timeline = []  # (frame, iss_active, data_index)

    def hook(d):
        if d.frame == 30:
            d.overhead.inject(FLIGHTS4)
        if d.frame == 700:
            d.overhead.inject(list(reversed(FLIGHTS4[:3])))
        d.overhead.iss_pass_data = make_iss(d.frame)
        timeline.append((d.frame, getattr(d, "_iss_active", False),
                         getattr(d, "_data_index", 0)))

    run_frames(d, 1500, hook)

    # slots: contiguous runs of iss_active True/False during the pass
    runs = []
    for f, a, idx in timeline:
        if PASS_START + 2 <= f < PASS_END:
            if runs and runs[-1][0] == a:
                runs[-1][1] += 1
            else:
                runs.append([a, 1])
    iss_slots = [n for a, n in runs if a]
    flight_slots = [n for a, n in runs if not a]
    check("dwell rotation: multiple ISS and flight slots alternate",
          len(iss_slots) >= 2 and len(flight_slots) >= 2,
          f"iss={len(iss_slots)} flight={len(flight_slots)}")
    # Thresholds come from the scene's own constants, so they hold at any frame
    # rate. They were 295 and 210 — the 10 fps values of DWELL_FRAMES and
    # CAMEO_MAX_FRAMES with a little slack — so at 15 fps the dwell check passed
    # trivially and the cameo cap check failed on a correct panel.
    from scenes.isspass import CAMEO_MAX_FRAMES, DWELL_FRAMES
    _slack = int(_frames.PER_SECOND)  # one second either way
    interior = iss_slots[:-1] if len(iss_slots) > 1 else iss_slots
    check("ISS slots last the 30s dwell",
          all(n >= DWELL_FRAMES - _slack for n in interior),
          f"slot lengths {iss_slots}")
    check("flight slots bounded by cameo cap (<=20s + slack)",
          all(n <= CAMEO_MAX_FRAMES + _slack for n in flight_slots[1:]),
          f"{flight_slots}")
    iss_total = sum(iss_slots); flight_total = sum(flight_slots)
    print(f"INFO  pass split: ISS {iss_total}f ({100*iss_total//(iss_total+flight_total)}%), "
          f"flights {flight_total}f across {len(flight_slots)} slots")
    # pages round-robin across flight slots (every plane gets seen)
    idx_seen = {idx for f, a, idx in timeline
                if PASS_START <= f < PASS_END and not a}
    check("multiple pages shown across flight slots", len(idx_seen) >= 2,
          f"indexes seen: {sorted(idx_seen)}")
    # ISS badge appears in the indicator zone during flight slots
    badge_px = [w for f, a, i in timeline if not a and PASS_START + 50 <= f < PASS_END
                for w in RECORDER.zone_writes(f, IND_X0, IND_X1, IND_Y0, IND_Y1)
                if w[2] == (100, 130, 180)]
    check("ISS badge drawn in indicator zone during flight slots",
          bool(badge_px), "no steel-blue writes in zone")
    # ...but "the badge appeared" is satisfied by a badge STUCK ON, which is the
    # likelier way this breaks. The zone has to ALTERNATE: page count on one
    # blink face, "ISS" on the other. Phase is int(time.time()//2)%2 in
    # scenes/flightdetails.py and the virtual clock puts frame 0 in phase 0, so
    # frame f should show the badge iff (f // ISS_BADGE_PHASE_FRAMES) % 2 == 1.
    # Constants are imported, not copied, so this tracks the app if the blink is
    # retuned or the colour changes.
    from scenes.flightdetails import ISS_BADGE_COLOUR, ISS_BADGE_PHASE_FRAMES
    _badge_rgb = (ISS_BADGE_COLOUR.red, ISS_BADGE_COLOUR.green, ISS_BADGE_COLOUR.blue)
    faces, seq = {}, []
    for f, a, i in timeline:          # timeline is in frame order
        if a or not (PASS_START + 50 <= f < PASS_END):
            continue
        cols = {w[2] for w in RECORDER.zone_writes(f, IND_X0, IND_X1, IND_Y0, IND_Y1)}
        if not cols:
            continue                  # draw-on-change: only flip frames write
        face = "ISS" if _badge_rgb in cols else "count"
        faces.setdefault((f // ISS_BADGE_PHASE_FRAMES) % 2, set()).add(face)
        seq.append(face)
    detail = "phase->faces " + repr({k: sorted(v) for k, v in sorted(faces.items())})
    check("indicator alternates count <-> ISS badge, each on its own blink face",
          faces.get(0) == {"count"} and faces.get(1) == {"ISS"}, detail)
    flips = sum(1 for x, y in zip(seq, seq[1:]) if x != y)
    check("badge blinks repeatedly, not once", flips >= 4, f"only {flips} face changes")
    check("takeover released after pass end",
          not [f for f, a, i in timeline if f > PASS_END + 1 and a], "")

elif SCENARIO == "isscap":
    # Starvation reproduction: NEW flight sets keep arriving during the
    # cameo, resetting the scroll cycle each time. Without a cameo cap the
    # ISS takeover never happens for the whole pass.
    PASS_START, PASS_END = 200, 1400

    def make_iss(frame):
        if PASS_START <= frame < PASS_END:
            p = (frame - PASS_START) / float(PASS_END - PASS_START)
            return {"is_active": True, "progress": p,
                    "time_remaining_sec": int((1 - p) * 120),
                    "rise_compass": "NW", "set_compass": "SE",
                    "max_elevation": 78}
        return None

    active_frames = set()

    def hook(d):
        # continuous churn from the start: genuinely NEW flights arrive
        # every 10s (distinct callsigns, picked up every 50 frames), so the
        # scroll position is reset before any cycle (~190 frames) completes
        if d.frame >= 10 and d.frame % 100 == 10:
            wave = d.frame // 100
            d.overhead.inject([
                flight(f"UAL{wave}0{i}", f"UA{wave}0{i}", "United Airlines",
                       "UAL", 45 + i) for i in range(3)])
        d.overhead.iss_pass_data = make_iss(d.frame)
        if getattr(d, "_iss_active", False):
            active_frames.add(d.frame)

    run_frames(d, 1500, hook)

    check("ISS takeover happens despite continuous flight churn",
          bool(active_frames), "ISS never took over — cameo starved it")
    if active_frames:
        t0 = min(active_frames)
        _secs = (t0 - PASS_START) / _frames.PER_SECOND
        print(f"INFO  takeover started at frame {t0} ({_secs:.0f}s into the pass)")
        check("takeover within 25s of pass start", _secs <= 25,
              f"took {_secs:.0f}s")

elif SCENARIO == "idle":
    # Idle-mode (clock page) flicker regression: with constant weather data
    # and no flights, the fixed scenes must stop rewriting unchanged pixels.
    run_frames(d, 900)
    SETTLE = 150  # first paints + one full alert/date cycle

    forecast_writes = {}   # frame -> writes in rows 12-32 (forecast band)
    temp_writes = {}       # frame -> writes in rows 0-5, x>=36 (temperature)
    clock_black = {}       # frame -> BLACK writes rows 0-5 x<36 (minute diff)
    date_black = {}        # frame -> BLACK writes rows 6-11 (date erases)
    for f in range(SETTLE, 900):
        for (x, y, c, ctx) in RECORDER.writes.get(f, []):
            if 12 <= y <= 32:
                forecast_writes.setdefault(f, []).append(ctx)
            if y <= 5 and x >= 36 and ctx != "loading_pulse":
                # loading_pulse's black keepalive at (63,0) is a same-value
                # overdraw — invisible, not flicker
                temp_writes.setdefault(f, []).append(ctx)
            if y <= 5 and x < 36 and c == (0, 0, 0):
                clock_black.setdefault(f, []).append(x)
            if 6 <= y <= 11 and c == (0, 0, 0):
                date_black.setdefault(f, []).append((x, ctx))

    check("forecast band untouched after initial paint (icons don't flicker)",
          not forecast_writes, f"{len(forecast_writes)} frames, e.g. "
          f"{list(forecast_writes.items())[:2]}")
    check("temperature untouched after initial paint",
          not temp_writes, f"{len(temp_writes)} frames, e.g. {list(temp_writes.items())[:2]}")
    # wall-clock minute rollovers are allowed: at most 2 in 75s, and each
    # erase must be small (changed glyph cells only, <= 3 chars * 4 cols)
    check("clock erases only on minute rollover, per-glyph only",
          len(clock_black) <= 2 and all(len(v) <= 60 for v in clock_black.values()),
          f"{len(clock_black)} frames, sizes {[len(v) for v in clock_black.values()][:5]}")
    check("no black erases in the date row (constant date, no tides)",
          not date_black, f"{len(date_black)} frames e.g. {list(date_black.items())[:2]}")
    check("forecast painted at least once (icons actually rendered)",
          any(y >= 17 for f in range(0, SETTLE)
              for (x, y, c, ctx) in RECORDER.writes.get(f, [])
              if ctx == "day" and c != (0, 0, 0)), "")

# ---- runtime errors ----------------------------------------------------------
if errors:
    print(f"\n=== {len(errors)} keyframe(s) raised ===")
    for k, tb in errors.items():
        print(f"--- {k} ---\n{tb}")
check("no keyframe exceptions", not errors, ", ".join(errors))

print()
print("ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
