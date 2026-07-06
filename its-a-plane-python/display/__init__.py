import sys
import os
from datetime import datetime
from setup import frames, screen
from utilities.animator import Animator
from utilities.overhead import Overhead

from scenes.temperature import TemperatureScene
from scenes.flightdetails import FlightDetailsScene
from scenes.flightlogo import FlightLogoScene
from scenes.journey import JourneyScene
from scenes.loadingpulse import LoadingPulseScene
from scenes.clock import ClockScene
from scenes.planedetails import PlaneDetailsScene
from scenes.daysforecast import DaysForecastScene
from scenes.date import DateScene
from scenes.trackedroute import TrackedRouteScene
from scenes.trackedprogress import TrackedProgressScene
from scenes.trackedstats import TrackedStatsScene
from scenes.isspass import ISSPassScene

from rgbmatrix import graphics
from rgbmatrix import RGBMatrix, RGBMatrixOptions


def flights_match(flights_a, flights_b):
    get_callsigns = lambda flights: [(f["callsign"], f["direction"]) for f in flights]
    updatable_a = set(get_callsigns(flights_a))
    updatable_b = set(get_callsigns(flights_b))
    return updatable_a == updatable_b


# A1 (from a1k): every flight page displays at least this long before
# advancing — prevents flashing through short entries. The scroll wrap is
# simply held (blank scroll lines; logo/journey/indicator stay up).
MIN_PAGE_FRAMES = int(frames.PER_SECOND * 10)

# Scroll sync: single shared position for both text lines.
# Both lines scroll together; the wider one determines when to reset/advance.


try:
    from config import (
        BRIGHTNESS,
        GPIO_SLOWDOWN,
        HAT_PWM_ENABLED,
        BRIGHTNESS_NIGHT,
        NIGHT_START,
        NIGHT_END,
        NIGHT_BRIGHTNESS,
        LED_RGB_SEQUENCE,
    )
    NIGHT_START = datetime.strptime(NIGHT_START, "%H:%M")
    NIGHT_END = datetime.strptime(NIGHT_END, "%H:%M")

except (ImportError, NameError):
    BRIGHTNESS = 100
    GPIO_SLOWDOWN = 1
    HAT_PWM_ENABLED = True
    NIGHT_BRIGHTNESS = False
    LED_RGB_SEQUENCE = "RGB"


def adjust_brightness(matrix):
    if NIGHT_BRIGHTNESS is False:
        return

    now = datetime.now().time().replace(second=0, microsecond=0)
    night_start_time = NIGHT_START.time().replace(second=0, microsecond=0)
    night_end_time = NIGHT_END.time().replace(second=0, microsecond=0)

    if night_end_time <= now < night_start_time:
        new_brightness = BRIGHTNESS
    else:
        new_brightness = BRIGHTNESS_NIGHT

    if matrix.brightness != new_brightness:
        matrix.brightness = new_brightness


class Display(
    TemperatureScene,
    FlightDetailsScene,
    FlightLogoScene,
    JourneyScene,
    LoadingPulseScene,
    PlaneDetailsScene,
    ClockScene,
    DaysForecastScene,
    TrackedRouteScene,
    TrackedProgressScene,
    TrackedStatsScene,
    DateScene,
    ISSPassScene,
    Animator,
):
    def __init__(self):
        options = RGBMatrixOptions()
        bonnet_type = os.environ.get("BONNET_TYPE", "single").lower()
        if bonnet_type == "triple":
            options.hardware_mapping = "regular"
        else:
            options.hardware_mapping = "adafruit-hat-pwm" if HAT_PWM_ENABLED else "adafruit-hat"
        options.rows = 32
        options.cols = 64
        options.chain_length = 1
        options.parallel = 1
        options.row_address_type = 0
        options.multiplexing = 0
        options.pwm_bits = 11
        options.brightness = BRIGHTNESS
        options.pwm_lsb_nanoseconds = 160
        options.led_rgb_sequence = LED_RGB_SEQUENCE
        options.pixel_mapper_config = ""
        options.show_refresh_rate = 0
        options.gpio_slowdown = GPIO_SLOWDOWN
        options.disable_hardware_pulsing = True
        options.drop_privileges = True
        options.limit_refresh_rate_hz = 120
        self.matrix = RGBMatrix(options=options)

        self.canvas = self.matrix.CreateFrameCanvas()
        self.canvas.Clear()

        self._data_index = 0
        self._data = []
        self._data_all_looped = False
        self._scroll_pos = screen.WIDTH
        self._page_started_frame = 0
        self._scroll_widths = {}  # region -> text width in pixels
        self._scroll_epoch_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), ".cache", "scroll_epoch.json")

        # Single Overhead instance handles both zone and tracked flight
        self.overhead = Overhead()
        self.overhead.grab_data()

        super().__init__()

        self.delay = frames.PERIOD

    def draw_square(self, x0, y0, x1, y1, colour):
        for x in range(x0, x1):
            _ = graphics.DrawLine(self.canvas, x, y0, x, y1, colour)

    @Animator.KeyFrame.add(0)
    def clear_screen(self):
        self.canvas.Clear()

    @Animator.KeyFrame.add(frames.PER_SECOND * 5)
    def check_for_loaded_data(self, count):
        if self.overhead.new_data:
            there_is_data = len(self._data) > 0 or not self.overhead.data_is_empty
            new_data = self.overhead.data
            data_is_different = not flights_match(self._data, new_data)

            if data_is_different:
                self._data_index = 0
                self._data_all_looped = False
                self._scroll_pos = screen.WIDTH
                self._page_started_frame = self.frame
                self._scroll_widths = {}
                self._write_scroll_epoch()
                # Reset ISS plane cameo flag when zone changes,
                # but only if ISS pass is NOT active (otherwise the
                # cameo would re-trigger on every data change)
                iss = self.overhead.iss_pass_data
                if not (iss and iss.get("is_active")):
                    self._iss_plane_shown = False
                self._data = new_data

            reset_required = there_is_data and data_is_different

            if reset_required:
                self.reset_scene()

    def report_scroll_width(self, region, width):
        """Called by each scroll scene to report its text width."""
        self._scroll_widths[region] = width

    @Animator.KeyFrame.add(1)
    def advance_scroll(self, count):
        """Single scroll driver for all text lines. Both lines share one position."""
        # During full ISS takeover the flight scenes neither draw nor report
        # widths; advancing would churn pages invisibly and blank the live
        # ISS frame via reset_scene(). (During the plane cameo _iss_active
        # is False, so the one allowed scroll cycle still runs.)
        if len(self._data) == 0 or getattr(self, "_iss_active", False):
            return

        # Decrement shared position
        self._scroll_pos -= 1

        # Check if widest text has fully scrolled off screen
        if not self._scroll_widths:
            return
        max_width = max(self._scroll_widths.values())
        if (self._scroll_pos + max_width < 0
                and self.frame - self._page_started_frame >= MIN_PAGE_FRAMES):
            # During ISS pass: after one full scroll cycle of plane data,
            # mark it as shown so ISS takeover resumes
            iss = self.overhead.iss_pass_data
            if iss and iss["is_active"] and len(self._data) > 0:
                self._iss_plane_shown = True

            if len(self._data) > 1:
                self._data_index = (self._data_index + 1) % len(self._data)
                self._data_all_looped = self._data_index == 0 or self._data_all_looped
                self._scroll_widths = {}
                self.reset_scene()
            self._scroll_pos = screen.WIDTH
            self._page_started_frame = self.frame
            self._write_scroll_epoch()

    def _write_scroll_epoch(self):
        """Write scroll start timestamp + text width for display mirror sync.

        pos: scroll position at write time (usually WIDTH on a cycle reset,
        but the frozen position when written at ISS-pass end) so the mirror
        can extrapolate from the true position instead of assuming 64.
        iss_plane_shown: lets the mirror replicate the plane cameo — show
        the flight display until the flag flips, then the ISS takeover.
        """
        try:
            import json, time
            max_w = max(self._scroll_widths.values()) if self._scroll_widths else 0
            with open(self._scroll_epoch_file, "w") as f:
                json.dump({"ts": time.time(), "idx": self._data_index,
                           "max_width": max_w, "pos": self._scroll_pos,
                           "cycle": max(max_w + screen.WIDTH + 1, MIN_PAGE_FRAMES),
                           "iss_plane_shown": getattr(self, "_iss_plane_shown", False)}, f)
        except Exception:
            pass

    @Animator.KeyFrame.add(1)
    def sync(self, count):
        _ = self.matrix.SwapOnVSync(self.canvas)
        adjust_brightness(self.matrix)

    @Animator.KeyFrame.add(frames.PER_SECOND * 30)
    def grab_new_data(self, count):
        # One call to overhead.grab_data() handles both zone scan
        # and tracked flight lookup (tracked only if zone is empty)
        if not self.overhead.processing and (
            self._data_all_looped or len(self._data) <= 1
        ):
            self.overhead.grab_data()

    def run(self):
        try:
            print("Press CTRL-C to stop")
            self.play()
        except KeyboardInterrupt:
            print("Exiting\n")
            sys.exit(0)
