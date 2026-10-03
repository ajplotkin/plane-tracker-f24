"""Render-loop timing.

The panel scrolls exactly one pixel per frame, so the frame rate IS the scroll
speed: 10 fps is 10 px/s, 15 fps is 15 px/s. On an LED grid the two cannot be
separated — there is no half-pixel step — so a higher rate reads as smoother
because each step is held for less time, and as faster because it is.

Set per device with PANEL_FPS in config/config.json (whole numbers, 8-20).
Read here directly rather than through config.py, which has import-time side
effects this module must not trigger: frames is imported by nearly every scene.
Both the display process and the web server read this file, and must agree —
a normal service restart restarts both.
"""
import json
import logging
import os

_DEFAULT_FPS = 10
_MIN_FPS, _MAX_FPS = 8, 20

_log = logging.getLogger(__name__)


def _configured_fps(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f).get("PANEL_FPS")
    except (OSError, ValueError, AttributeError):
        return _DEFAULT_FPS
    if raw is None or raw == "":
        return _DEFAULT_FPS
    try:
        fps = float(raw)
    except (TypeError, ValueError):
        fps = None
    # Whole numbers only. Every keyframe divisor is PER_SECOND times something,
    # and Animator.KeyFrame.add refuses a fractional divisor rather than let it
    # fire at the wrong rate — so a fractional fps would stop the panel at
    # import. Out-of-range values fall back rather than fail: a typo in a web
    # form must not take a remote Pi off the air.
    if fps is None or fps != int(fps) or not _MIN_FPS <= fps <= _MAX_FPS:
        _log.warning("PANEL_FPS=%r is not a whole number in %d-%d; using %d",
                     raw, _MIN_FPS, _MAX_FPS, _DEFAULT_FPS)
        return _DEFAULT_FPS
    return int(fps)


PER_SECOND = _configured_fps()
PERIOD = 1 / PER_SECOND
