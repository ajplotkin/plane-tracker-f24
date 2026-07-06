"""End-to-end headless debug of the its-a-plane display pipeline.

Runs the REAL display module (all scenes, real animator, real fonts) against
a faithful rgbmatrix stub, drives synthetic multi-flight data through the
whole loop, and checks the flicker mechanism at frame granularity:
writes into the page-indicator zone must occur ONLY when the page changes.

Usage: python e2e_debug.py <workdir> <scenario>
  scenario: multi | single | reset | iss
"""
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

    # data becomes active at frame 50 (check_for_loaded_data divisor)
    DATA_ACTIVE = 50
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
    # interior ISS slots run the full dwell (~300 frames)
    interior = iss_slots[:-1] if len(iss_slots) > 1 else iss_slots
    check("ISS slots last the 30s dwell", all(n >= 295 for n in interior),
          f"slot lengths {iss_slots}")
    check("flight slots bounded by cameo cap (<=20s + slack)",
          all(n <= 210 for n in flight_slots[1:]), f"{flight_slots}")
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
        print(f"INFO  takeover started at frame {t0} "
              f"({(t0 - PASS_START) / 10.0:.0f}s into the pass)")
        check("takeover within 25s of pass start", t0 - PASS_START <= 250,
              f"took {(t0 - PASS_START) / 10.0:.0f}s")

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
