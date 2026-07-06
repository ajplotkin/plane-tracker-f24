import time
from datetime import datetime
from PIL import Image

from utilities.animator import Animator
from setup import colours, fonts, frames, screen
from utilities.temperature import grab_forecast
from rgbmatrix import graphics

# Setup
DAY_COLOUR = colours.LIGHT_PINK
MIN_T_COLOUR = colours.LIGHT_MID_BLUE
MAX_T_COLOUR = colours.LIGHT_DARK_ORANGE
TEXT_FONT = fonts.extrasmall
FONT_HEIGHT = 5
DISTANCE_FROM_TOP = 32
ICON_SIZE = 10
FORECAST_SIZE = FONT_HEIGHT * 2 + ICON_SIZE
DAY_POSITION = DISTANCE_FROM_TOP - FONT_HEIGHT - ICON_SIZE
ICON_POSITION = DISTANCE_FROM_TOP - FONT_HEIGHT - ICON_SIZE
TEMP_POSITION = DISTANCE_FROM_TOP
FETCH_RETRY_SECONDS = 60

# Icon PNGs decoded once per icon name: (width, height, ((x, y, r, g, b)…))
# or None if the file is missing. Re-decoding + LANCZOS resampling on every
# repaint took tens of ms on a Pi — long enough for the panel refresh to
# show blank/partial icons.
_ICON_CACHE = {}


def _icon_pixels(icon):
    if icon in _ICON_CACHE:
        return _ICON_CACHE[icon]
    try:
        image = Image.open(f"icons/{icon}.png")
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.ANTIALIAS
        image.thumbnail((ICON_SIZE, ICON_SIZE), resample)
        rgb = image.convert("RGB")
        pixels = rgb.load()
        w, h = rgb.size
        data = tuple((px, py, *pixels[px, py])
                     for py in range(h) for px in range(w))
        _ICON_CACHE[icon] = (w, h, data)
    except FileNotFoundError:
        _ICON_CACHE[icon] = None
    return _ICON_CACHE[icon]


class DaysForecastScene(object):
    def __init__(self):
        super().__init__()
        self._redraw_forecast = True
        self._last_hour = None
        self._cached_forecast = None
        self._forecast_signature = None
        self._fetch_retry_after = 0.0

        # Pre-load forecast from disk cache (survives reboots).
        # Concept from c0wsaysmoo/plane-tracker-rgb-pi.
        try:
            import time as _time
            from utilities.temperature import _load_file_cache, _FORECAST_CACHE_FILE
            cached, ts = _load_file_cache(_FORECAST_CACHE_FILE)
            if cached and (_time.time() - ts) < 7200:  # 2-hour TTL
                self._cached_forecast = cached
        except Exception:
            pass

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def day(self, count):
        if getattr(self, '_iss_active', False):
            self._redraw_forecast = True
            return

        # --- SCENE SWITCH HANDLING ---
        # Plane overhead — clear and yield
        if len(self._data):
            self._redraw_forecast = True
            return

        # Tracked flight is live — yield to TrackedFlightScene
        # Only clear once when transitioning from forecast to tracked
        if self.overhead.tracked_data is not None:
            if not self._redraw_forecast:
                self.draw_square(0, 12, 64, 32, colours.BLACK)
                self._redraw_forecast = True
            return

        # Refresh the cache at most once per hour, with a retry backoff
        # after failures. Fetch BEFORE touching the canvas — the old code
        # cleared the live rows first, leaving the bottom of the panel
        # black for the whole (blocking) HTTP round-trip.
        current_hour = datetime.now().hour
        if self._cached_forecast is None or self._last_hour != current_hour:
            if time.time() >= self._fetch_retry_after:
                forecast = grab_forecast(tag="days")
                if forecast:
                    self._cached_forecast = forecast
                    self._last_hour = current_hour
                    self._fetch_retry_after = 0.0
                else:
                    # Keep whatever is cached; retry in a minute instead of
                    # re-attempting (and previously re-clearing) every second
                    self._fetch_retry_after = time.time() + FETCH_RETRY_SECONDS

        if not self._cached_forecast:
            return  # nothing to draw yet; canvas untouched

        # Build the render list and repaint only when the CONTENT changes
        # (a few times a day) or the scene re-enters after being wiped.
        # The canvas is live — clearing + repainting an identical forecast
        # every hour was the visible weather-icon flicker.
        entries = self._forecast_entries(self._cached_forecast)
        signature = tuple(entries)
        if signature == self._forecast_signature and not self._redraw_forecast:
            return

        self.draw_square(0, 12, 64, 32, colours.BLACK)
        self._forecast_signature = signature
        self._redraw_forecast = False

        offset = 1
        space_width = screen.WIDTH // 3

        for day_name, icon, min_temp, max_temp in entries:
            # --- Centering Calculations ---
            min_temp_width = len(min_temp) * 4
            max_temp_width = len(max_temp) * 4

            temp_gap = 2  # pixels between high and low temps
            temp_x = offset + (space_width - min_temp_width - max_temp_width - temp_gap) // 2 + 1
            max_temp_x = temp_x
            min_temp_x = temp_x + max_temp_width + temp_gap

            icon_x = offset + (space_width - ICON_SIZE) // 2
            day_x = offset + (space_width - 12) // 2 + 1

            # --- Draw to Matrix ---
            graphics.DrawText(self.canvas, TEXT_FONT, day_x, DAY_POSITION, DAY_COLOUR, day_name)

            cached_icon = _icon_pixels(icon)
            if cached_icon:
                _, _, pixel_data = cached_icon
                for px, py, r, g, b in pixel_data:
                    self.canvas.SetPixel(px + icon_x, py + ICON_POSITION, r, g, b)

            graphics.DrawText(self.canvas, TEXT_FONT, max_temp_x, TEMP_POSITION, MAX_T_COLOUR, max_temp)
            graphics.DrawText(self.canvas, TEXT_FONT, min_temp_x, TEMP_POSITION, MIN_T_COLOUR, min_temp)

            offset += space_width

            if offset >= screen.WIDTH:
                break

    @staticmethod
    def _forecast_entries(forecast):
        """(day_name, icon, min_temp, max_temp) per visible day.

        Skips entries before the local date ("Midnight Switch"), so the
        signature changes — and triggers a repaint — at midnight even
        without a fetch.
        """
        today_local = datetime.now().astimezone().date()
        entries = []
        for day in forecast:
            local_time = datetime.fromisoformat(day["startTime"])
            if local_time.date() < today_local:
                continue
            entries.append((
                local_time.strftime("%a"),
                day["values"]["weatherCodeFullDay"],
                f"{day['values']['temperatureMin']:.0f}",
                f"{day['values']['temperatureMax']:.0f}",
            ))
            if len(entries) == 3:
                break
        return entries
