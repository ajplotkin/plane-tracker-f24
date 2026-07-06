from PIL import Image

from utilities.animator import Animator
from setup import colours

LOGO_SIZE = 16
DEFAULT_IMAGE = "default"


def _draw_image_on_canvas(canvas, image, x_offset=0, y_offset=0):
    """Draw a PIL image pixel-by-pixel (avoids Pillow/rgbmatrix unsafe_ptrs crash)."""
    rgb = image.convert("RGB")
    pixels = rgb.load()
    width, height = rgb.size
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            canvas.SetPixel(x + x_offset, y + y_offset, r, g, b)


class FlightLogoScene:
    def __init__(self):
        super().__init__()
        self._logo_cache_icao = None
        self._logo_cache_pixels = None

    @Animator.KeyFrame.add(0)
    def logo_details(self):

        # Guard against no data or ISS takeover
        if len(self._data) == 0 or getattr(self, '_iss_active', False):
            return

        icao = self._data[self._data_index]["owner_icao"]
        if icao in ("", "N/A"):
            icao = DEFAULT_IMAGE

        # Only reload + redraw when airline changes
        if icao != self._logo_cache_icao:
            try:
                image = Image.open(f"logos/{icao}.png")
            except Exception:  # missing, corrupt, or unreadable file
                try:
                    image = Image.open(f"logos/{DEFAULT_IMAGE}.png")
                except Exception:
                    image = None
            if image is None:
                self._logo_cache_pixels = None
                self._logo_cache_icao = icao
                return

            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.ANTIALIAS
            image.thumbnail((LOGO_SIZE, LOGO_SIZE), resample)
            self._logo_cache_pixels = image.convert("RGB")
            self._logo_cache_icao = icao

        # ALWAYS draw: this keyframe only runs at frame 0 / inside
        # reset_scene(), i.e. right after clear_screen() blacked the
        # canvas. The old _logo_drawn flag skipped the redraw whenever a
        # reset kept the same operator (same flight refreshed, or two
        # consecutive pages with the same icao) — leaving no logo at all.
        # The icao cache above still avoids reloading the PNG.
        if self._logo_cache_pixels:
            # Clear first in case the logo is smaller than 16x16
            self.draw_square(0, 0, LOGO_SIZE, LOGO_SIZE, colours.BLACK)
            _draw_image_on_canvas(self.canvas, self._logo_cache_pixels)
