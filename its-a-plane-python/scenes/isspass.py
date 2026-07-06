"""
ISS Overhead Pass — full display takeover scene.

When the ISS is actively overhead (1-6 minutes), this scene takes over the
entire display with an animated ISS sprite, progress bar, and countdown.

Layout (32x64 LED matrix):
  Rows  0-4:  "ISS OVERHEAD" blinking text
  Rows  5-12: ISS sprite moving left-to-right (position = pass progress)
              with dim trail dots behind it
  Row   16:   Direction + elevation text (e.g., "NW > SE  88°")
  Rows 22-24: Progress bar (dashed: green flown, blue remaining, + marker)
  Rows 27-31: Countdown text (e.g., "3:42 LEFT")
"""

import logging
import os

from PIL import Image

from utilities.animator import Animator
from setup import colours, fonts, screen, frames
from rgbmatrix import graphics

logger = logging.getLogger(__name__)


# Fonts
TITLE_FONT = fonts.extrasmall       # 4x6
INFO_FONT = fonts.extrasmall        # 4x6
COUNTDOWN_FONT = fonts.small        # 5x8

# Colour themes — warm (visible) vs cool (not visible)
THEME_VISIBLE = {
    "title": colours.WHITE,
    "title_dim": colours.LIGHT_GREY,
    "trail": graphics.Color(60, 50, 20),        # gold
    "flown": colours.LIMEGREEN,
    "remaining": colours.LIGHT_BLUE,
    "marker": colours.WHITE,
    "info": colours.LIGHT_ORANGE,
    "countdown": colours.YELLOW,
}

THEME_DIM = {
    "title": graphics.Color(100, 130, 180),      # steel blue
    "title_dim": graphics.Color(50, 65, 90),      # dark blue
    "trail": graphics.Color(30, 35, 60),           # dim navy
    "flown": graphics.Color(40, 100, 100),         # dim teal
    "remaining": graphics.Color(60, 60, 70),       # dark grey
    "marker": graphics.Color(140, 140, 160),       # muted white
    "info": graphics.Color(120, 120, 130),          # slate grey
    "countdown": graphics.Color(80, 160, 170),      # dim cyan
}

# Layout positions
TITLE_Y = 5           # baseline for "ISS OVERHEAD"
SPRITE_Y = 6          # top of sprite region (sprite is 8px tall)
SPRITE_MID_Y = 10     # vertical center of sprite for trail dots
INFO_Y = 20           # baseline for direction + elevation
PROGRESS_Y = 23       # center row of progress bar
COUNTDOWN_Y = 31      # baseline for countdown text

# ISS sprite
_ISS_IMAGE = None
_ISS_W = 0
_ISS_H = 0


def _load_iss_sprite():
    """Load ISS.png once, return (pixels, width, height)."""
    global _ISS_IMAGE, _ISS_W, _ISS_H
    if _ISS_IMAGE is not None:
        return _ISS_IMAGE, _ISS_W, _ISS_H
    try:
        img_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logos", "ISS_STATION.png")
        img = Image.open(img_path).convert("RGBA")
        _ISS_IMAGE = img.load()
        _ISS_W, _ISS_H = img.size
    except Exception as e:
        logger.warning(f"Failed to load ISS sprite: {e}")
        _ISS_IMAGE = None
        _ISS_W, _ISS_H = 0, 0
    return _ISS_IMAGE, _ISS_W, _ISS_H


def _draw_plus_marker(canvas, x, y, colour):
    """Draw a + shaped marker (like trackedprogress.py plane marker)."""
    canvas.SetPixel(x, y, colour.red, colour.green, colour.blue)
    canvas.SetPixel(x - 1, y, colour.red, colour.green, colour.blue)
    canvas.SetPixel(x + 1, y, colour.red, colour.green, colour.blue)
    canvas.SetPixel(x, y - 1, colour.red, colour.green, colour.blue)
    canvas.SetPixel(x, y + 1, colour.red, colour.green, colour.blue)


# Progress bar geometry (module-level: shared by draw + restore helpers)
BAR_START = 2
BAR_WIDTH = screen.WIDTH - 4  # leave 2px margin each side

# Hard cap on the plane-cameo at pass start. The cameo normally ends when a
# full scroll cycle completes, but every flight-list change resets the
# scroll position — with continuously changing traffic the cycle never
# completes and the ISS takeover would be starved for the whole pass.
CAMEO_MAX_FRAMES = int(frames.PER_SECOND * 20)  # 20 s

# Dwell rotation: during a pass with flights overhead, alternate
# ISS takeover (DWELL) with one flight page per interlude, so every
# plane still gets seen across the pass and the ISS keeps recurring,
# sustained screen time.
DWELL_FRAMES = int(frames.PER_SECOND * 30)  # 30 s per ISS slot


def _bar_dash_visible(x):
    """Dashed line: draw every other 2px group (x is bar-relative)."""
    return (x // 2) % 2 == 0


class ISSPassScene(object):
    def __init__(self):
        super().__init__()
        self._iss_plane_shown = False
        self._iss_was_active = False
        self._iss_active = False  # checked by other scenes to yield
        self._iss_render = None   # draw-state cache; None = full redraw needed
        self._iss_cameo_start = None  # keyframe count when the cameo began
        self._iss_takeover_start = None  # keyframe count when takeover began
        self._iss_pass_active = False  # read by flightdetails for the badge

    def _bar_pixel_colour(self, x, flown_px, theme):
        """Colour of bar-relative pixel x on the progress row, or None (gap)."""
        if 0 <= x < BAR_WIDTH and _bar_dash_visible(x):
            return theme["flown"] if x < flown_px else theme["remaining"]
        return None

    @staticmethod
    def _write_iss_live(visible):
        """Mirror contract: live in-shadow visibility, recomputed here every
        second — the pass-level `visible` flag in iss.json is fixed at the
        culmination-time value and can't flip mid-pass."""
        try:
            import json, time
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                ".cache", "iss_live.json")
            with open(path, "w") as f:
                json.dump({"visible": bool(visible), "ts": time.time()}, f)
        except Exception:
            pass

    @Animator.KeyFrame.add(1)
    def iss_pass_scene(self, count):
        iss = self.overhead.iss_pass_data
        self._iss_pass_active = bool(iss and iss["is_active"])
        if not self._iss_pass_active:
            self._iss_active = False
            self._iss_render = None
            if self._iss_was_active:
                # Pass just ended. reset_scene() wipes the ISS pixels via
                # clear_screen AND re-runs the draw-once scenes (journey,
                # journey_arrow, logo) that would otherwise stay black
                # until the next flight-set change.
                self._iss_was_active = False
                self._iss_plane_shown = False
                self._iss_cameo_start = None
                self._iss_takeover_start = None
                self.reset_scene()
                # Refresh the mirror's scroll sync: ts/pos were frozen for
                # the whole takeover, and iss_plane_shown just flipped.
                self._write_scroll_epoch()
            return

        self._iss_was_active = True

        # During ISS pass: allow ONE plane scroll cycle, then suppress.
        # Hard cap: flight-list changes reset the scroll cycle, so with
        # continuously changing traffic it may never complete — after
        # CAMEO_MAX_FRAMES the takeover proceeds regardless.
        if len(self._data) > 0 and not self._iss_plane_shown:
            if self._iss_cameo_start is None:
                self._iss_cameo_start = count
            elif count - self._iss_cameo_start >= CAMEO_MAX_FRAMES:
                self._iss_plane_shown = True

        if len(self._data) > 0 and not self._iss_plane_shown:
            if self._iss_active:
                # Takeover -> flight-slot transition: re-run the draw-once
                # scenes (their guards need _iss_active False first), start
                # the page from the screen edge, and publish the slot flip
                # so the web mirror follows.
                self._iss_active = False
                self.reset_scene()
                self._scroll_pos = screen.WIDTH
                self._page_started_frame = self.frame
                self._write_scroll_epoch()
            self._iss_active = False  # let other scenes draw during cameo
            self._iss_render = None
            return

        takeover_started = not self._iss_active
        self._iss_active = True  # suppress other scenes
        self._iss_cameo_start = None
        if takeover_started:
            self._iss_takeover_start = count
            # Publish iss_plane_shown (and the frozen scroll pos) so the
            # web mirror flips from cameo to takeover in sync.
            self._write_scroll_epoch()

        # Dwell rotation: after DWELL_FRAMES of takeover with flights
        # waiting, hand the screen back for one flight page. advance_scroll
        # re-raises _iss_plane_shown at that page's cycle end, so the slots
        # alternate for the rest of the pass (page index round-robins).
        if (len(self._data) > 0 and self._iss_takeover_start is not None
                and count - self._iss_takeover_start >= DWELL_FRAMES):
            self._iss_plane_shown = False
            self._iss_cameo_start = None
            # finish this frame as ISS; next frame takes the flight branch

        # The canvas is live (sync() discards SwapOnVSync's return), so a
        # full Clear()+redraw every frame is visible as whole-panel shimmer.
        # Instead: clear + draw everything once on entry or theme change,
        # then per frame rewrite only the elements whose value changed.
        progress = iss["progress"]
        time_remaining = iss["time_remaining_sec"]
        render = self._iss_render
        second = count // int(frames.PER_SECOND)

        # Real-time visibility check (warm theme if visible, cool if not),
        # recomputed at most once per second — it runs ephem math.
        if render is not None and render["second"] == second:
            visible = render["visible"]
        else:
            from utilities.iss import is_iss_visible_now
            import config as cfg
            visible = is_iss_visible_now(cfg.LOCATION_HOME[0], cfg.LOCATION_HOME[1])
        theme = THEME_VISIBLE if visible else THEME_DIM

        # Element states this frame
        blink_phase = second % 2
        title_text = "ISS VISIBLE" if visible else "ISS OVERHEAD"
        title_x = max(0, (screen.WIDTH - len(title_text) * 4) // 2)
        pixels, sprite_w, sprite_h = _load_iss_sprite()
        usable_width = screen.WIDTH - sprite_w
        sprite_x = max(0, min(usable_width, int(progress * usable_width)))
        flown_px = int(progress * BAR_WIDTH)
        marker_x = BAR_START + min(flown_px, BAR_WIDTH - 1)
        mins = time_remaining // 60
        secs = time_remaining % 60
        countdown_text = f"{mins}:{secs:02d} LEFT"
        countdown_x = max(0, (screen.WIDTH - len(countdown_text) * 5) // 2)

        def draw_title():
            colour = theme["title"] if blink_phase == 0 else theme["title_dim"]
            graphics.DrawText(self.canvas, TITLE_FONT, title_x, TITLE_Y,
                              colour, title_text)

        def draw_trail_and_sprite():
            trail = theme["trail"]
            for tx in range(0, sprite_x, 2):
                self.canvas.SetPixel(tx, SPRITE_MID_Y,
                                     trail.red, trail.green, trail.blue)
            if pixels:
                for py in range(sprite_h):
                    for px in range(sprite_w):
                        r, g, b, a = pixels[px, py]
                        if a > 0:
                            self.canvas.SetPixel(sprite_x + px, SPRITE_Y + py, r, g, b)

        def draw_info():
            info_text = f"{iss['rise_compass']}>{iss['set_compass']} {int(iss['max_elevation'])}\xb0"
            info_x = max(0, (screen.WIDTH - len(info_text) * 4) // 2)
            graphics.DrawText(self.canvas, INFO_FONT, info_x, INFO_Y,
                              theme["info"], info_text)

        def draw_bar_range(x0, x1):
            for x in range(max(0, x0), min(BAR_WIDTH, x1)):
                colour = self._bar_pixel_colour(x, flown_px, theme)
                if colour:
                    self.canvas.SetPixel(BAR_START + x, PROGRESS_Y,
                                         colour.red, colour.green, colour.blue)

        def draw_countdown():
            graphics.DrawText(self.canvas, COUNTDOWN_FONT, countdown_x,
                              COUNTDOWN_Y, theme["countdown"], countdown_text)

        if render is None or render["visible"] != visible:
            # Full redraw: takeover entry, or theme flip
            self._write_iss_live(visible)
            self.canvas.Clear()
            draw_title()
            draw_trail_and_sprite()
            draw_info()
            draw_bar_range(0, BAR_WIDTH)
            _draw_plus_marker(self.canvas, marker_x, PROGRESS_Y, theme["marker"])
            draw_countdown()
        else:
            # Incremental: rewrite only what changed
            if blink_phase != render["blink_phase"]:
                draw_title()  # same text/position — pure colour swap

            if sprite_x != render["sprite_x"]:
                # black out the old sprite box, then trail dots + new sprite
                self.draw_square(render["sprite_x"], SPRITE_Y,
                                 render["sprite_x"] + sprite_w,
                                 SPRITE_Y + sprite_h - 1, colours.BLACK)
                draw_trail_and_sprite()

            if flown_px != render["flown_px"]:
                lo = min(flown_px, render["flown_px"])
                hi = max(flown_px, render["flown_px"])
                draw_bar_range(lo, hi)

            if marker_x != render["marker_x"]:
                # restore the bar under the old marker, then draw the new one
                old = render["marker_x"]
                for mx in (old - 1, old, old + 1):
                    colour = self._bar_pixel_colour(mx - BAR_START, flown_px, theme)
                    if colour:
                        self.canvas.SetPixel(mx, PROGRESS_Y,
                                             colour.red, colour.green, colour.blue)
                    else:
                        self.canvas.SetPixel(mx, PROGRESS_Y, 0, 0, 0)
                self.canvas.SetPixel(old, PROGRESS_Y - 1, 0, 0, 0)
                self.canvas.SetPixel(old, PROGRESS_Y + 1, 0, 0, 0)
                _draw_plus_marker(self.canvas, marker_x, PROGRESS_Y, theme["marker"])

            if countdown_text != render["countdown_text"]:
                # erase the old text (DrawText in black), draw the new
                graphics.DrawText(self.canvas, COUNTDOWN_FONT,
                                  render["countdown_x"], COUNTDOWN_Y,
                                  colours.BLACK, render["countdown_text"])
                draw_countdown()

        self._iss_render = {
            "second": second,
            "visible": visible,
            "blink_phase": blink_phase,
            "sprite_x": sprite_x,
            "flown_px": flown_px,
            "marker_x": marker_x,
            "countdown_text": countdown_text,
            "countdown_x": countdown_x,
        }
