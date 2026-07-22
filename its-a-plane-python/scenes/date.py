import logging
from datetime import datetime
from utilities.temperature import grab_forecast
from utilities.animator import Animator
from setup import colours, fonts, frames
from rgbmatrix import graphics

# Setup
DATE_FONT = fonts.extrasmall
DATE_POSITION = (36, 11)

# Tide colors
TIDE_HIGH_COLOUR = graphics.Color(0, 255, 255)     # Cyan
TIDE_LOW_COLOUR = graphics.Color(66, 164, 244)      # Light blue
WATER_TEMP_COLOUR = graphics.Color(0, 200, 150)    # Teal
WATER_TEMP_FALLBACK_COLOUR = graphics.Color(100, 160, 200)  # Blue-grey (fallback indicator)

# Sea-temp icon (6px wide, 5px tall), drawn left of the number in the x36-63
# rotation slot in place of the "Sea " text label — the label left no room for a
# space, so a coastal reading ran together with the number.
SEA_ICON = ("......", ".##..#", "#..##.", ".##..#", "#..##.")   # double wave
ICON_WIDTH = 6
ICON_Y_TOP = 6                                        # aligns with 4x6 digits (y6-10)
ICON_NUMBER_X = DATE_POSITION[0] + ICON_WIDTH + 2     # 36 + 6 + 2 = 44
_ICON_TYPES = {"water": SEA_ICON, "water_fb": SEA_ICON}


def _draw_water_icon(canvas, icon, colour):
    """Draw a 6x5 water-temp icon at x36 (rows ICON_Y_TOP..+4)."""
    x0 = DATE_POSITION[0]
    for r, row in enumerate(icon):
        for c, ch in enumerate(row):
            if ch == "#":
                canvas.SetPixel(x0 + c, ICON_Y_TOP + r,
                                colour.red, colour.green, colour.blue)

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

        # Build display items: date always, tides + water temp if available
        tides = self._get_tides()
        items = [("date", current_date)]
        if tides:
            if tides.get("high"):
                items.append(("high", f"H{tides['high']}"))
            if tides.get("low"):
                items.append(("low", f"L{tides['low']}"))
            # Water temp after tides (same coastal context)
            # Color shifts to blue-grey when reading is from a fallback station
            try:
                from utilities.tides import get_water_temp, is_water_temp_fallback
                wt = get_water_temp()
                if wt:
                    wtype = "water_fb" if is_water_temp_fallback() else "water"
                    # Number only; the SEA_ICON is drawn to its left in date().
                    items.append((wtype, f"{wt}\xb0"))
            except Exception:
                pass

        # Pick current item based on cycle
        cycle_len = len(items) * _CYCLE_SECONDS
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

        # Clear the previous item. Sea items draw an icon at x36 plus the number
        # shifted to ICON_NUMBER_X, so a black text-redraw at x36 would leave the
        # icon and shifted number lit — clear the whole slot instead.
        needs_clear = (
            (self._last_display_text and self._last_display_text != display_text)
            or (getattr(self, "_redraw_date", False) and self._last_display_text)
        )
        if needs_clear:
            if self._last_item_type in _ICON_TYPES:
                self.draw_square(DATE_POSITION[0], ICON_Y_TOP, 64, 11, colours.BLACK)
            else:
                graphics.DrawText(self.canvas, DATE_FONT, DATE_POSITION[0],
                                  DATE_POSITION[1], colours.BLACK, self._last_display_text)

        self._last_display_text = display_text
        self._last_date = current_date
        self._last_item_type = item_type

        # Draw with appropriate colour. Sea items draw an icon at x36 and the
        # number at ICON_NUMBER_X; everything else is plain text at x36.
        if item_type in _ICON_TYPES:
            icon_colour = (WATER_TEMP_FALLBACK_COLOUR if item_type == "water_fb"
                           else WATER_TEMP_COLOUR)
            _draw_water_icon(self.canvas, _ICON_TYPES[item_type], icon_colour)
            graphics.DrawText(self.canvas, DATE_FONT, ICON_NUMBER_X,
                              DATE_POSITION[1], icon_colour, display_text)
        elif item_type == "date":
            self.draw_gradient_text(display_text, DATE_POSITION[0], DATE_POSITION[1], start_color, end_color)
        elif item_type == "high":
            graphics.DrawText(self.canvas, DATE_FONT, DATE_POSITION[0], DATE_POSITION[1], TIDE_HIGH_COLOUR, display_text)
        elif item_type == "low":
            graphics.DrawText(self.canvas, DATE_FONT, DATE_POSITION[0], DATE_POSITION[1], TIDE_LOW_COLOUR, display_text)

        self._redraw_date = False
