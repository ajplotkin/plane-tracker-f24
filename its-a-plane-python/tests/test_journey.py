"""Journey scene: the arrow must track the origin text width, not sit at a
hard-fixed x that a 4+ char code (or a junk string like "UNKNOWN") overruns.

Regression for the photographed bug: origin "UNKNOWN" with the ">" arrow stamped
over its middle. rgbmatrix is stubbed in tests/conftest.py.
"""
from unittest.mock import MagicMock, patch

import scenes.journey as J
from scenes.journey import (
    JourneyScene, JOURNEY_POSITION, ARROW_WIDTH, ARROW_POINT_POSITION,
)


def _scene(origin, destination="LAX"):
    s = JourneyScene.__new__(JourneyScene)
    s._last_debug_print = None
    s._journey_arrow_point_x = ARROW_POINT_POSITION[0]   # the __init__ default
    s.canvas = MagicMock()
    s.draw_square = lambda *a, **k: None
    s._data = [{
        "origin": origin, "destination": destination,
        "distance_origin": 100.0, "distance_destination": 200.0,
        "time_estimated_arrival": None, "time_scheduled_arrival": 0,
        "time_real_departure": None, "time_scheduled_departure": 0,
    }]
    s._data_index = 0
    return s


def _run(s):
    # DrawText returns the pixel advance (7px/char, the regularplus font) so the
    # arrow position is computed from a realistic origin width.
    with patch.object(J.graphics, "DrawText",
                      side_effect=lambda canvas, font, x, y, col, t: len(t) * 7):
        s.journey()


def test_arrow_tracks_origin_width():
    s3 = _scene("EWR")
    _run(s3)
    assert s3._journey_arrow_point_x == JOURNEY_POSITION[0] + 3 * 7 + ARROW_WIDTH   # 43

    s4 = _scene("KJFK")   # 4-char ICAO: used to collide with the fixed x37-42
    _run(s4)
    assert s4._journey_arrow_point_x == JOURNEY_POSITION[0] + 4 * 7 + ARROW_WIDTH   # 50
    assert s4._journey_arrow_point_x > 42   # no longer pinned over the 4th glyph


def test_arrow_never_overlaps_the_origin_text():
    for origin in ("EWR", "KJFK", "UNKNOWN"):   # incl. the junk string
        s = _scene(origin)
        _run(s)
        origin_end = JOURNEY_POSITION[0] + len(origin) * 7
        arrow_base = s._journey_arrow_point_x - ARROW_WIDTH
        assert arrow_base >= origin_end, (
            f"arrow at {arrow_base} overlaps origin {origin!r} ending {origin_end}")
