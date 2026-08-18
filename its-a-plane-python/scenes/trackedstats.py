from utilities.animator import Animator
from utilities.landmarks import get_nearest_landmark
from setup import colours, fonts, screen
from config import DISTANCE_UNITS
from rgbmatrix import graphics

# Optional configs — defaults for when not set
try:
    from config import SPEED_UNITS
except (ImportError, ModuleNotFoundError, NameError):
    SPEED_UNITS = "knots"

try:
    from config import CLOCK_FORMAT
except (ImportError, ModuleNotFoundError, NameError):
    CLOCK_FORMAT = "24hr"

LINE3_Y = 31
FONT = fonts.small

# Time remaining + distance remaining
TIME_DIST_COLOUR = colours.LIGHT_MID_BLUE

# Aircraft type, altitude, speed
STATS_COLOUR    = colours.LIGHT_PINK
AIRCRAFT_COLOUR = colours.LIGHT_PINK
CITY_COLOUR     = colours.WHITE

# Departure delay colouring for a pre-departure tracked flight.
# utilities/airport_status.py grades AIRPORT-WIDE FAA delays at 45/90/120 min;
# a delay to the ONE flight you are waiting for matters at a smaller size, so
# the same three-step yellow/orange/red ladder is shifted down.
DELAY_THRESHOLD_MIN = 15   # below this, not worth the panel width
_DELAY_BANDS = (
    (90, colours.LIGHT_RED),
    (45, colours.LIGHT_ORANGE),
    (DELAY_THRESHOLD_MIN, colours.LIGHT_YELLOW),
)

# Cache nearest city result — only recalculate when position changes significantly
_city_cache = {"lat": None, "lon": None, "result": None}
_CITY_CACHE_THRESHOLD = 0.01  # ~1km — recalculate when plane moves this far


def _delay_colour(minutes):
    """Colour for a departure delay, or None when it is too small to show."""
    for threshold, colour in _DELAY_BANDS:
        if minutes >= threshold:
            return colour
    return None


def _format_delay(minutes):
    """'+45m' under an hour, '+1:45' at or above. Kept short for 64px."""
    if minutes < 60:
        return f"(+{minutes}m)"
    return f"(+{minutes // 60}:{minutes % 60:02d})"


def _format_altitude(altitude):
    """Format altitude as flight level (FL180+) or feet below transition altitude."""
    if not altitude:
        return None
    altitude = int(altitude)
    if altitude >= 18000:
        fl = altitude // 100
        return f"FL{fl:03d}"
    else:
        return f"{altitude:,}ft"


def _format_speed(ground_speed):
    if not ground_speed:
        return None, None
    if SPEED_UNITS == "imperial":
        mph = ground_speed * 1.15078
        return f"{int(mph)}", "mph"
    elif SPEED_UNITS == "metric":
        kph = ground_speed * 1.852
        return f"{int(kph)}", "km/h"
    else:  # knots default
        return f"{int(ground_speed)}", "knts"


def _format_dep_time(dep_time_str):
    """Format departure time from '2026-05-11 18:30'. Respects CLOCK_FORMAT."""
    if not dep_time_str:
        return ""
    try:
        parts = dep_time_str.split(" ")
        if len(parts) < 2:
            return dep_time_str
        hm = parts[1].split(":")
        hour = int(hm[0])
        minute = int(hm[1]) if len(hm) > 1 else 0

        if CLOCK_FORMAT == "12hr":
            ampm = "a" if hour < 12 else "p"
            display_hour = hour % 12 or 12
            if minute:
                return f"{display_hour}:{minute:02d}{ampm}"
            return f"{display_hour}{ampm}"
        else:
            return f"{hour}:{minute:02d}"
    except (ValueError, IndexError):
        return dep_time_str


def _build_stats(data):
    """
    Build list of (text, colour) tuples for the stats line.
    Live:      1:23 234mi nr Atlanta B738 FL350↑ 260mph
    Scheduled: Departs 6:30p EWR→LAX
    Delayed:   Departs 8:15p (+1:45) EWR→LAX  (time + delay in the delay colour)
    """
    parts = []

    # Scheduled (pre-departure) — show departure info instead of live stats
    if data.get("is_scheduled"):
        # dep_delay_min is None when AirLabs published no delay information, in
        # which case this renders exactly as it did before the delay feature.
        delay_min = data.get("dep_delay_min")
        delay_colour = _delay_colour(delay_min) if delay_min else None
        revised = data.get("dep_time_revised") or ""

        # Show the revised time only when we actually have one \u2014 a delay whose
        # size is known but whose new time is not still shows the scheduled time.
        dep_src = revised if (delay_colour and revised) else data.get("dep_time", "")
        dep = _format_dep_time(dep_src)

        origin = data.get("origin", "")
        dest = data.get("destination", "")

        if not dep:
            for ch in f"Scheduled {origin}\u2192{dest}":
                parts.append((ch, TIME_DIST_COLOUR))
            return parts

        for ch in "Departs ":
            parts.append((ch, TIME_DIST_COLOUR))
        for ch in dep:
            parts.append((ch, delay_colour or TIME_DIST_COLOUR))
        if delay_colour:
            for ch in " " + _format_delay(delay_min):
                parts.append((ch, delay_colour))
        for ch in f" {origin}\u2192{dest}":
            parts.append((ch, TIME_DIST_COLOUR))
        return parts

    # Time remaining
    if data.get("time_remaining"):
        for ch in data["time_remaining"]:
            parts.append((ch, TIME_DIST_COLOUR))
        parts.append((" ", STATS_COLOUR))

    # Distance remaining
    if data.get("dist_remaining") is not None:
        unit = "km" if DISTANCE_UNITS == "metric" else "mi"
        dist_str = f"{int(data['dist_remaining'])}{unit}"
        for ch in dist_str:
            parts.append((ch, TIME_DIST_COLOUR))
        parts.append((" ", STATS_COLOUR))

    # Nearest city (cached — only recalculate when position changes)
    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat is not None and lon is not None:
        if (_city_cache["lat"] is None
                or abs(lat - _city_cache["lat"]) > _CITY_CACHE_THRESHOLD
                or abs(lon - _city_cache["lon"]) > _CITY_CACHE_THRESHOLD):
            _city_cache["lat"] = lat
            _city_cache["lon"] = lon
            _city_cache["result"] = get_nearest_landmark(lat, lon)
        nearest = _city_cache["result"]
        if nearest:
            for ch in f"nr {nearest['name']}":
                parts.append((ch, CITY_COLOUR))
            parts.append((" ", STATS_COLOUR))

    # Aircraft type
    aircraft = data.get("aircraft_type", "")
    if aircraft and aircraft not in ("", "N/A"):
        for ch in aircraft:
            parts.append((ch, AIRCRAFT_COLOUR))
        parts.append((" ", STATS_COLOUR))

    # Altitude + vertical speed arrow
    alt_str = _format_altitude(data.get("altitude"))
    if alt_str:
        for ch in alt_str:
            parts.append((ch, STATS_COLOUR))
        # Vertical speed arrow immediately after
        vs = data.get("vertical_speed", 0) or 0
        if vs > 64:
            parts.append(("\u2191", colours.LIGHT_GREEN))
        elif vs < -64:
            parts.append(("\u2193", colours.LIGHT_LIGHT_RED))
        parts.append((" ", STATS_COLOUR))

    # Speed (no space between value and unit)
    spd_val, spd_unit = _format_speed(data.get("ground_speed"))
    if spd_val:
        for ch in spd_val:
            parts.append((ch, STATS_COLOUR))
        for ch in spd_unit:
            parts.append((ch, STATS_COLOUR))

    return parts


class TrackedStatsScene(object):
    def __init__(self):
        super().__init__()
        self._ts_pos = screen.WIDTH
        self._ts_len = 0
        self._ts_last_number = None

    @Animator.KeyFrame.add(0)
    def reset_tracked_stats_scroll(self):
        self._ts_pos = screen.WIDTH
        self._ts_len = 0

    @Animator.KeyFrame.add(1)
    def tracked_stats(self, count):
        if getattr(self, '_iss_active', False):
            return
        if len(self._data) > 0:
            return

        tracked = self.overhead.tracked_data
        if not tracked:
            return

        # New tracked flight: restart the scroll (reset_scene only fires on
        # zone-flight changes, not tracked-flight changes).
        _num = tracked.get("number") or tracked.get("callsign")
        if _num != self._ts_last_number:
            self._ts_pos = screen.WIDTH
            self._ts_len = 0
            self._ts_last_number = _num

        char_list = _build_stats(tracked)

        # Clear row
        self.draw_square(0, LINE3_Y - 6, screen.WIDTH, LINE3_Y, colours.BLACK)

        # Draw scrolling text
        total_len = 0
        for ch, colour in char_list:
            w = graphics.DrawText(
                self.canvas, FONT,
                self._ts_pos + total_len, LINE3_Y,
                colour, ch,
            )
            total_len += w
        self._ts_len = total_len

        self._ts_pos -= 1

        if self._ts_pos + self._ts_len < 0:
            self._ts_pos = screen.WIDTH
            # Write epoch for display mirror scroll sync
            try:
                import json, time, os
                cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
                with open(os.path.join(cache_dir, "tracked_stats_epoch.json"), "w") as f:
                    json.dump({"ts": time.time(), "width": self._ts_len}, f)
            except Exception:
                pass
