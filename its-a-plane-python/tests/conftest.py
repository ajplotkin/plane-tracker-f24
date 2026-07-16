"""Shared pytest setup: stub the Pi-only `rgbmatrix` C extension before any test
module imports scene/setup code.

`graphics` is a MagicMock (auto-provides Font / DrawText / DrawLine / etc.)
EXCEPT `graphics.Color`, which is a REAL class so colour constants are distinct
objects. A bare MagicMock returns ONE shared singleton for every `Color(...)`
call, which makes identity comparisons between colour constants (e.g. the EPA
AQI bands in scenes/temperature.py) vacuously true — every `is` assertion passes
no matter what the logic returns. A real Color keeps setup/colours.py's
constants distinguishable so those tests can actually fail when they should.

conftest.py is imported by pytest before any test module, so this wins over the
per-file `if "rgbmatrix" not in sys.modules` stubs (they see it already set and
skip).
"""
import sys
import types
from unittest.mock import MagicMock


class _Color:
    """Minimal stand-in for rgbmatrix.graphics.Color (red/green/blue)."""
    __slots__ = ("red", "green", "blue")

    def __init__(self, red=0, green=0, blue=0):
        self.red, self.green, self.blue = red, green, blue

    def __eq__(self, other):
        return (isinstance(other, _Color)
                and (self.red, self.green, self.blue)
                == (other.red, other.green, other.blue))

    def __hash__(self):
        return hash((self.red, self.green, self.blue))


if "rgbmatrix" not in sys.modules:
    _m = types.ModuleType("rgbmatrix")
    _g = MagicMock()
    _g.Color = _Color
    _m.graphics = _g
    _m.RGBMatrix = MagicMock
    _m.RGBMatrixOptions = MagicMock
    sys.modules["rgbmatrix"] = _m
