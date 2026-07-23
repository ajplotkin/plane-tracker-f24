from datetime import datetime, timedelta
from rgbmatrix import graphics
from utilities.animator import Animator
from setup import colours, fonts, frames
from utilities.temperature import grab_temperature_and_humidity
try:
    # get_current_uv() interpolates the hourly forecast UV curve to "now" so the
    # chip doesn't lag the 30-min realtime snapshot (falls back to it internally).
    from utilities.temperature import get_current_uv
except ImportError:
    get_current_uv = lambda: None
try:
    from utilities.air_quality import get_aqi
except ImportError:
    get_aqi = lambda: None

# Scene Setup
TEMPERATURE_REFRESH_SECONDS = 600
TEMPERATURE_FONT = fonts.extrasmall
TEMPERATURE_FONT_HEIGHT = 5
# AQI "haze/particulate" glyph (4px wide, 5px tall) drawn left of the number in
# place of the old "A" prefix. Rows map to y0-4, aligned with the 4x6 digits.
AQI_HAZE_ICON = ("....", "#.#.", ".#.#", "#.#.", "....")
# Official EPA AQI category colours (airnow.gov). Kept as their own constants
# (not the shared palette, which UV etc. use) so AQI matches the standard exactly.
AQI_GOOD = graphics.Color(0, 228, 0)            # #00E400
AQI_MODERATE = graphics.Color(255, 255, 0)      # #FFFF00
AQI_USG = graphics.Color(255, 126, 0)           # #FF7E00  Unhealthy for Sensitive Groups
AQI_UNHEALTHY = graphics.Color(255, 0, 0)       # #FF0000
AQI_VERY_UNHEALTHY = graphics.Color(143, 63, 151)  # #8F3F97
AQI_MAROON = graphics.Color(126, 0, 35)         # #7E0023  Hazardous


def _aqi_colour(aqi):
    """Official EPA AQI category colour (airnow.gov)."""
    if aqi > 300:   return AQI_MAROON            # Hazardous
    if aqi > 200:   return AQI_VERY_UNHEALTHY    # Very Unhealthy
    if aqi > 150:   return AQI_UNHEALTHY         # Unhealthy
    if aqi > 100:   return AQI_USG               # Unhealthy for Sensitive Groups
    if aqi > 50:    return AQI_MODERATE          # Moderate
    return AQI_GOOD                              # Good
# (Night-boundary redraws removed: adjust_brightness() acts at panel level,
# pixel content needs no repaint.)

class TemperatureScene(object):
    def __init__(self):
        super().__init__()
        self._last_temperature = None
        self._last_temperature_str = None
        self._last_temp_colour = None
        self._last_updated = None
        self._cached_temp = None
        self._cached_humidity = None
        self._redraw_temp = True
        self._last_uv_draw = None
        self._last_aqi_draw = None

    def _draw_aqi_icon(self, x0, colour):
        """Draw the 4x5 AQI haze glyph at x0 (rows y0-4, aligned with the digits)."""
        for r, row in enumerate(AQI_HAZE_ICON):
            for c, ch in enumerate(row):
                if ch == "#":
                    self.canvas.SetPixel(x0 + c, r, colour.red, colour.green, colour.blue)

    def colour_gradient(self, colour_A, colour_B, ratio):
        return graphics.Color(
            int(colour_A.red + ((colour_B.red - colour_A.red) * ratio)),
            int(colour_A.green + ((colour_B.green - colour_A.green) * ratio)),
            int(colour_A.blue + ((colour_B.blue - colour_A.blue) * ratio)),
        )

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def temperature(self, count):
        if getattr(self, '_iss_active', False):
            self._redraw_temp = True
            return

        # Ensure redraw when there's new data
        if len(self._data):
            self._redraw_temp = True
            return

        # Determine seconds since last update
        seconds_since_update = (datetime.now() - self._last_updated).total_seconds() if self._last_updated else TEMPERATURE_REFRESH_SECONDS
        retry_interval_on_error = 60

        # Determine if we need to fetch new data
        need_fetch = (
            seconds_since_update >= TEMPERATURE_REFRESH_SECONDS or
            (self._cached_temp is None and (self._last_updated is None or seconds_since_update >= retry_interval_on_error))
        )

        if need_fetch:
            current_temperature, current_humidity = grab_temperature_and_humidity()
            if current_temperature is not None and current_humidity is not None:
                self._cached_temp = (current_temperature, current_humidity)
                self._last_updated = datetime.now()
            else:
                # Failed — keep showing the cached value (if any) and retry
                # in a minute instead of hammering the API every second
                self._last_updated = datetime.now() - timedelta(
                    seconds=TEMPERATURE_REFRESH_SECONDS - retry_interval_on_error)

        # Determine display string and colour from the freshest good data
        if self._cached_temp:
            current_temperature, current_humidity = self._cached_temp
            display_str = f"{round(current_temperature)}°"
            humidity_ratio = current_humidity / 100.0
            temp_colour = self.colour_gradient(colours.WHITE, colours.DARK_BLUE, humidity_ratio)
        else:
            current_temperature = None
            display_str = "ERR"
            temp_colour = colours.RED

        # UV chip: shown right-aligned on the temp row instead of taking
        # a slot in the alert rotation. EPA/WHO colours.
        try:
            uv = get_current_uv()
        except Exception:
            uv = None
        uv_int = max(1, int(round(uv))) if uv is not None and uv > 0 else 0
        if uv_int >= 11:  uv_colour = colours.PURPLE
        elif uv_int >= 8: uv_colour = colours.RED
        elif uv_int >= 6: uv_colour = colours.LIGHT_ORANGE
        elif uv_int >= 3: uv_colour = colours.YELLOW
        else:             uv_colour = colours.GREEN
        # "UV2" fits beside a 4-char temp ("102°" ends x51; 12px chip
        # starts x52); two-digit values drop the V ("U11") to stay 12px.
        uv_str = ("" if not uv_int
                  else f"UV{uv_int}" if uv_int < 10 else f"U{uv_int}")

        # AQI chip: colour-coded "A<nnn>" in the gap between the time and the
        # temp (x20-35), shown when AQI >= the configured threshold. Keyless —
        # see utilities/air_quality.py for the source and its fallback.
        try:
            from config import AQI_ALERTS_ENABLED, AQI_THRESHOLD
        except ImportError:
            AQI_ALERTS_ENABLED, AQI_THRESHOLD = False, 50
        aqi = None
        if AQI_ALERTS_ENABLED:
            try:
                aqi = get_aqi()
            except Exception:
                aqi = None
        if aqi is not None and aqi >= AQI_THRESHOLD:
            # Number only; the AQI_HAZE_ICON is drawn to its left (replaces "A").
            aqi_str, aqi_colour = f"{aqi}", _aqi_colour(aqi)
        else:
            aqi_str, aqi_colour = "", colours.GREEN

        # Draw only on change — the canvas is live (sync() discards
        # SwapOnVSync's return), so erasing+redrawing identical content
        # every second is visible flicker.
        colour_key = (temp_colour.red, temp_colour.green, temp_colour.blue,
                      uv_str, uv_colour.red, aqi_str, aqi_colour.red)
        if (not self._redraw_temp
                and display_str == self._last_temperature_str
                and colour_key == self._last_temp_colour):
            return

        # Left-justify at x=36 (aligned with date/tide below)
        TEMPERATURE_POSITION = (36, TEMPERATURE_FONT_HEIGHT)

        # Erase the old string only when the text itself changed (a pure
        # colour change overdraws the same glyph pixels). Black DrawText
        # erases exactly the old glyphs — including the first one at x=36,
        # which the old draw_square(40,...) clear always missed.
        if (self._last_temperature_str is not None
                and display_str != self._last_temperature_str):
            graphics.DrawText(
                self.canvas,
                TEMPERATURE_FONT,
                TEMPERATURE_POSITION[0],
                TEMPERATURE_POSITION[1],
                colours.BLACK,
                self._last_temperature_str,
            )

        graphics.DrawText(
            self.canvas,
            TEMPERATURE_FONT,
            TEMPERATURE_POSITION[0],
            TEMPERATURE_POSITION[1],
            temp_colour,
            display_str,
        )

        # UV chip, right-aligned to x=63 (rows 0-5; clear of the loading
        # pulse at (63,0) since 4x6 glyphs leave their 4th column empty)
        if self._last_uv_draw:
            old_str, old_x = self._last_uv_draw
            graphics.DrawText(self.canvas, TEMPERATURE_FONT, old_x,
                              TEMPERATURE_FONT_HEIGHT, colours.BLACK, old_str)
            self._last_uv_draw = None
        if uv_str:
            uv_x = 64 - 4 * len(uv_str)
            graphics.DrawText(self.canvas, TEMPERATURE_FONT, uv_x,
                              TEMPERATURE_FONT_HEIGHT, uv_colour, uv_str)
            self._last_uv_draw = (uv_str, uv_x)

        # AQI chip: a haze glyph + the number, right-aligned to x=35 in the gap
        # after the time, before the temp at x=36. glyph(4) + up to 3 digits(12)
        # = 16px max -> starts x20, clear of the 4-5 char time (never past x19).
        # _last_aqi_draw stores (number_str, icon_x); the number sits at icon_x+4.
        if self._last_aqi_draw:
            old_str, old_x = self._last_aqi_draw
            graphics.DrawText(self.canvas, TEMPERATURE_FONT, old_x + 4,
                              TEMPERATURE_FONT_HEIGHT, colours.BLACK, old_str)
            self._draw_aqi_icon(old_x, colours.BLACK)
            self._last_aqi_draw = None
        if aqi_str:
            aqi_x = 36 - (4 + 4 * len(aqi_str))
            self._draw_aqi_icon(aqi_x, aqi_colour)
            graphics.DrawText(self.canvas, TEMPERATURE_FONT, aqi_x + 4,
                              TEMPERATURE_FONT_HEIGHT, aqi_colour, aqi_str)
            self._last_aqi_draw = (aqi_str, aqi_x)

        self._last_temperature_str = display_str
        self._last_temperature = current_temperature
        self._last_temp_colour = colour_key
        self._redraw_temp = False
