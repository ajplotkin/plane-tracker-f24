import logging
from datetime import datetime
from utilities.temperature import grab_forecast
from utilities.animator import Animator
from setup import colours, fonts, frames
from rgbmatrix import graphics

try:
    from utilities.pool_temp import get_pool_temp, get_pool_status
except ImportError:
    get_pool_temp = lambda: None
    get_pool_status = lambda: None

try:
    from utilities.beach_conditions import get_beach_conditions
except ImportError:
    get_beach_conditions = lambda: None

# Setup
DATE_FONT = fonts.extrasmall
DATE_POSITION = (36, 11)

# Tide colors
TIDE_HIGH_COLOUR = graphics.Color(0, 255, 255)     # Cyan
TIDE_LOW_COLOUR = graphics.Color(66, 164, 244)      # Light blue
WATER_TEMP_COLOUR = graphics.Color(0, 200, 150)    # Teal
WATER_TEMP_FALLBACK_COLOUR = graphics.Color(100, 160, 200)  # Blue-grey (fallback indicator)
POOL_TEMP_COLOUR = graphics.Color(0, 191, 255)     # Pool blue (deep sky blue)
POOL_HEAT_COLOUR = graphics.Color(255, 138, 30)    # Orange (heater actively running)
POOL_COOL_COLOUR = graphics.Color(120, 200, 255)   # Ice blue (declining: heater/pump off)

# Icons drawn at x36 in the x36-63 rotation slot, replacing a text label that
# left no room for a space. Water temp (6px) replaces "Sea "/"Pool" (so "Pool70°"
# no longer reads as "170"); tide arrows (4px) replace the H/L letter.
SEA_ICON = ("......", ".##..#", "#..##.", ".##..#", "#..##.")   # double wave
POOL_ICON = ("#....#", "######", "#....#", "######", ".####.")  # ladder
TIDE_UP_ICON = (".#..", "###.", ".#..", ".#..", ".#..")         # high tide (rising)
TIDE_DOWN_ICON = (".#..", ".#..", ".#..", "###.", ".#..")       # low tide (falling)
# Beach flag (EH Town lifeguard report): a pennant on a pole (6px), coloured by the
# flag — green/yellow/red; the swell height ("3ft") follows it in the same colour.
BEACH_FLAG_ICON = ("#.....", "######", "#####.", "####..", "#.....")
BEACH_GREEN  = graphics.Color(0, 200, 0)
BEACH_YELLOW = graphics.Color(255, 190, 0)
BEACH_RED    = graphics.Color(255, 45, 45)
# Flame (6px) trailing the temp at FLAME_X while the pool/spa heater is running;
# down arrow (same slot) while the pool is declining (heater off, or pump/flow off).
FLAME_ICON = ("..#...", ".###..", ".###..", "#####.", ".###..")
POOL_COOL_ICON = (".#..", ".#..", ".#..", "###.", ".#..")   # down arrow (matches falling-tide)
FLAME_X = 56                                          # fixed; fits after a 2- or 3-digit temp
ICON_WIDTH = 6                                        # water icon width (sea/pool)
ICON_Y_TOP = 6                                        # aligns with 4x6 digits (y6-10)
ICON_NUMBER_X = DATE_POSITION[0] + ICON_WIDTH + 2     # 36 + 6 + 2 = 44 (water)
# type -> (icon, x where the number/time starts). 6px water icons leave a 2px gap
# (x44); the 4px tide arrows sit flush at x40 so the full "11:07p" time still fits.
# "pool_heat" is the pool item while heating: same ladder + temp, plus a flame.
_ICON_TYPES = {
    "water":     (SEA_ICON, ICON_NUMBER_X),
    "water_fb":  (SEA_ICON, ICON_NUMBER_X),
    "pool":      (POOL_ICON, ICON_NUMBER_X),
    "pool_heat": (POOL_ICON, ICON_NUMBER_X),
    "pool_cool": (POOL_ICON, ICON_NUMBER_X),
    "high":      (TIDE_UP_ICON, DATE_POSITION[0] + 4),
    "low":       (TIDE_DOWN_ICON, DATE_POSITION[0] + 4),
    "beach_green":  (BEACH_FLAG_ICON, ICON_NUMBER_X),
    "beach_yellow": (BEACH_FLAG_ICON, ICON_NUMBER_X),
    "beach_red":    (BEACH_FLAG_ICON, ICON_NUMBER_X),
}
_ICON_COLOUR = {
    "water": WATER_TEMP_COLOUR, "water_fb": WATER_TEMP_FALLBACK_COLOUR,
    "pool": POOL_TEMP_COLOUR, "pool_heat": POOL_TEMP_COLOUR, "pool_cool": POOL_TEMP_COLOUR,
    "high": TIDE_HIGH_COLOUR, "low": TIDE_LOW_COLOUR,
    "beach_green": BEACH_GREEN, "beach_yellow": BEACH_YELLOW, "beach_red": BEACH_RED,
}
# Trailing indicator at FLAME_X: heating types get an orange flame, cooling types a
# blue down arrow. (Add "spa_heat"/"spa_cool" etc. for a future hot tub.)
_HEATING_TYPES = {"pool_heat"}
_COOLING_TYPES = {"pool_cool"}

# Cycle timing: 5 seconds per item (called once per second)
_CYCLE_SECONDS = 5

class DateScene(object):
    def __init__(self):
        super().__init__()
        self._last_date = None
        self._last_display_text = None  # track what's currently drawn for clearing
        self._redraw_date = False
        self.today_moonphase = None
        self.last_fetched_moonphase = None
        self._cycle_counter = 0  # increments each second
        self._date_suppressed = False
        self._cached_tides = None
        self._tide_fetch_date = None
        self._last_item_type = None  # drives icon-aware clearing


    def moonphase(self):
        now = datetime.now()

        # Only fetch forecast if it's a new day
        if self.last_fetched_moonphase != now.day:
            try:
                forecast = grab_forecast(tag="DateScene")
                if not forecast:
                    logging.error("Forecast data missing or API error (moon phase).")
                    return self.today_moonphase

                for day in forecast:
                    forecast_date = day['startTime'][:10]
                    if forecast_date == now.strftime('%Y-%m-%d'):
                        utc_moonphase = int(day["values"]["moonPhase"])
                        self.today_moonphase = utc_moonphase
                        self.last_fetched_moonphase = now.day
                        break

            except Exception as e:
                logging.error(f"Error fetching forecast for moon phase: {e}")
                return self.today_moonphase

        return self.today_moonphase

    def map_moon_phase_to_color(self, moonphase):
        colors = [
            [colours.DARK_PURPLE, colours.DARK_PURPLE],
            [colours.DARK_PURPLE, colours.DARK_MID_PURPLE],
            [colours.DARK_PURPLE, colours.WHITE],
            [colours.DARK_MID_PURPLE, colours.WHITE],
            [colours.GREY, colours.GREY],
            [colours.WHITE, colours.DARK_MID_PURPLE],
            [colours.WHITE, colours.DARK_PURPLE],
            [colours.DARK_MID_PURPLE, colours.DARK_PURPLE],
        ]
        moonphase = min(max(moonphase, 0), 7)
        return colors[moonphase]

    def draw_gradient_text(self, text, x, y, start_color, end_color):
        text_length = len(text)
        char_width = 4
        for i, char in enumerate(text):
            position = i / max(1, text_length - 1)
            r = int(start_color.red + (end_color.red - start_color.red) * position)
            g = int(start_color.green + (end_color.green - start_color.green) * position)
            b = int(start_color.blue + (end_color.blue - start_color.blue) * position)
            char_color = graphics.Color(r, g, b)
            char_x = x + (i * char_width)
            _ = graphics.DrawText(
                self.canvas,
                DATE_FONT,
                char_x,
                y,
                char_color,
                char,
            )

    def _get_tides(self):
        """Fetch tide data once per day, cached."""
        today = str(datetime.now().date())
        if self._tide_fetch_date == today and self._cached_tides is not None:
            return self._cached_tides
        try:
            from utilities.tides import get_next_tides
            self._cached_tides = get_next_tides()
            self._tide_fetch_date = today
        except Exception:
            self._cached_tides = None
        return self._cached_tides

    def _build_rotation_items(self, current_date):
        """The rotating right-side items: date, then (if available) high/low
        tide, sea temp, and pool temp. Returns a list of (type, text) tuples;
        the type drives the colour (in date()) and the mirror colour map."""
        items = [("date", current_date)]

        tides = self._get_tides()
        if tides:
            if tides.get("high"):
                # Time only; the TIDE_UP_ICON (rising arrow) is drawn to its left.
                items.append(("high", f"{tides['high']}"))
            if tides.get("low"):
                # Time only; the TIDE_DOWN_ICON (falling arrow) is drawn to its left.
                items.append(("low", f"{tides['low']}"))

        # Beach flag + swell (EH Town lifeguard report) — its own rotation item,
        # placed here so it lands third from the bottom of the full rotation (before
        # the sea + pool temps). "beach_<flag>" drives a green/yellow/red pennant;
        # the text is the swell height ("3ft"). Independent of tides.
        try:
            from config import BEACH_REPORT_ENABLED
        except ImportError:
            BEACH_REPORT_ENABLED = False
        if BEACH_REPORT_ENABLED:
            try:
                bc = get_beach_conditions()
            except Exception:
                bc = None
            if (bc and bc.get("flag") in ("green", "yellow", "red")
                    and bc.get("swell_ft") is not None):
                # Require a swell value: the text must be non-empty (an empty rotation
                # text defeats the slot-clear guards -> stale pixels). int(sw + 0.5) is
                # half-up (Python round() is half-to-even: 2.5 -> "2ft").
                items.append((f"beach_{bc['flag']}", f"{int(bc['swell_ft'] + 0.5)}ft"))

        # Sea (ocean) temp — after the beach flag, same coastal context. Colour
        # shifts to blue-grey when the reading is from a fallback station. Kept
        # gated on tides being available (the original coastal grouping).
        if tides:
            try:
                from utilities.tides import get_water_temp, is_water_temp_fallback
                wt = get_water_temp()
                if wt:
                    wtype = "water_fb" if is_water_temp_fallback() else "water"
                    # Number only; the SEA_ICON is drawn to its left in date().
                    items.append((wtype, f"{wt}\xb0"))
            except Exception:
                pass

        # Pool temp (Home Assistant) — grouped with the sea temp / tides. Config
        # read fresh (a config-page save reloads config) so the toggle takes
        # effect without a restart.
        try:
            from config import POOL_TEMP_ENABLED
        except ImportError:
            POOL_TEMP_ENABLED = False
        if POOL_TEMP_ENABLED:
            try:
                pt = get_pool_temp()
            except Exception:
                pt = None
            if pt is not None:
                # Number only; the POOL_ICON (ladder) is drawn to its left in date().
                # A trailing indicator follows the temp: flame while actively heating,
                # a down arrow while declining (heater off, or pump off), nothing while
                # holding at setpoint (or when the heater state is unknown).
                try:
                    status = get_pool_status()
                except Exception:
                    status = None
                pool_type = ("pool_heat" if status == "heating"
                             else "pool_cool" if status == "declining"
                             else "pool")
                items.append((pool_type, f"{round(pt)}\xb0"))

        return items

    def _slot_needs_clear(self, item_type, display_text):
        """Whether to clear the x36-63 slot before drawing. Clears on a text OR
        item-type change — two icon types can share a display string (sea "70°"
        == pool "70°"), so a text-only check would leave stale pixels of the old
        icon — and on a forced scene re-entry (_redraw_date)."""
        if not self._last_display_text:
            return False
        if getattr(self, "_redraw_date", False):
            return True
        return (self._last_display_text != display_text
                or self._last_item_type != item_type)

    def _draw_slot_icon(self, icon, colour, x0=DATE_POSITION[0]):
        """Draw a small icon at x0 (rows ICON_Y_TOP..+4) — water/tide icon at x36,
        or the heater flame at FLAME_X."""
        for r, row in enumerate(icon):
            for c, ch in enumerate(row):
                if ch == "#":
                    self.canvas.SetPixel(x0 + c, ICON_Y_TOP + r,
                                         colour.red, colour.green, colour.blue)

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def date(self, count):
        if getattr(self, '_iss_active', False):
            self._redraw_date = True
            return

        # Flights active: the flight scenes own the display. Return before
        # the overflow block below — a stale _alert_overflow from idle mode
        # would otherwise stamp black over journey's rows 6-11.
        if len(self._data):
            self._redraw_date = True
            return

        # Suppress date when alert text overflows into date area.
        # _alert_overflow is the alert char count (0 = no overflow).
        # Counter is PAUSED while suppressed so each item gets its full
        # visibility window.  On the transition back to visible, snap to
        # the next item boundary so a fresh item starts immediately.
        overflow_chars = getattr(self, '_alert_overflow', 0)
        was_suppressed = getattr(self, '_date_suppressed', False)
        if overflow_chars > 0:
            if self._last_display_text:
                alert_end_x = overflow_chars * 4
                clear_start = max(alert_end_x, DATE_POSITION[0])
                if clear_start < 64:
                    self.draw_square(clear_start, 6, 64, 11, colours.BLACK)
                self._last_display_text = None
            self._date_suppressed = True
            self._redraw_date = True
            return

        # Transition from suppressed → visible: advance to next item
        if was_suppressed:
            self._cycle_counter = ((self._cycle_counter // _CYCLE_SECONDS) + 1) * _CYCLE_SECONDS
            self._date_suppressed = False

        self._cycle_counter += 1

        now = datetime.now()
        current_date = now.strftime("%b %d")

        # Build the rotating right-side items (date, tides, sea temp, pool temp)
        items = self._build_rotation_items(current_date)

        # Pick current item based on cycle
        slot = (self._cycle_counter // _CYCLE_SECONDS) % len(items)
        item_type, display_text = items[slot]

        # Write date display state for mirror: slot + items + timing for interpolation
        try:
            import json, os, time as _time
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
            with open(os.path.join(cache_dir, "date_display.json"), "w") as f:
                json.dump({"type": item_type, "text": display_text,
                           "slot": slot, "total": len(items),
                           "items": [{"type": t, "text": tx} for t, tx in items],
                           "cycle_secs": _CYCLE_SECONDS, "ts": _time.time()}, f)
        except Exception:
            pass

        # Get moon phase colors (used for date, neutral for tides)
        moon_phase_value = self.moonphase()
        if moon_phase_value is None:
            start_color = end_color = colours.RED
        else:
            start_color, end_color = self.map_moon_phase_to_color(moon_phase_value)

        # Clear previous text if it changed
        # Clear the previous item. Icon items (sea/pool) occupy the icon at x36
        # plus the number shifted to ICON_NUMBER_X, so a black text-redraw at x36
        # would leave the icon and the shifted number lit — clear the whole slot.
        needs_clear = self._slot_needs_clear(item_type, display_text)
        if needs_clear:
            if self._last_item_type in _ICON_TYPES:
                self.draw_square(DATE_POSITION[0], ICON_Y_TOP, 64, 11, colours.BLACK)
            else:
                graphics.DrawText(self.canvas, DATE_FONT, DATE_POSITION[0],
                                  DATE_POSITION[1], colours.BLACK, self._last_display_text)

        self._last_display_text = display_text
        self._last_date = current_date
        self._last_item_type = item_type

        # Draw with appropriate colour. Icon items (sea/pool temp, tide high/low)
        # draw an icon at x36 and the number/time at the type's number-x; the date
        # is a per-char gradient; anything else is plain text at x36.
        if item_type in _ICON_TYPES:
            icon, number_x = _ICON_TYPES[item_type]
            icon_colour = _ICON_COLOUR[item_type]
            self._draw_slot_icon(icon, icon_colour)
            graphics.DrawText(self.canvas, DATE_FONT, number_x,
                              DATE_POSITION[1], icon_colour, display_text)
            if item_type in _HEATING_TYPES:      # heater running: flame after the temp
                self._draw_slot_icon(FLAME_ICON, POOL_HEAT_COLOUR, x0=FLAME_X)
            elif item_type in _COOLING_TYPES:    # declining: down arrow after the temp
                self._draw_slot_icon(POOL_COOL_ICON, POOL_COOL_COLOUR, x0=FLAME_X)
        elif item_type == "date":
            self.draw_gradient_text(display_text, DATE_POSITION[0], DATE_POSITION[1], start_color, end_color)

        self._redraw_date = False
