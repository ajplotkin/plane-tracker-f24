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
import time
import types
from unittest.mock import MagicMock


def settle(module, timeout=5.0):
    """Wait for `module`'s background refresh (if one is in flight) to finish.

    The non-blocking fetchers (air_quality, rain,
    tides — same pattern as airport_status/nws_alerts/iss) dispatch a worker
    thread and return the LAST GOOD value immediately, so a test that asserts on
    a freshly fetched value has to wait for that worker.

    `_refresh_pending` is cleared in the worker's `finally`, inside
    `_refresh_lock` and after the module globals have been updated — so once it
    reads False the refresh is complete and its results are visible. The lock is
    then taken briefly to be sure the worker has actually exited it.

    Always call this INSIDE the `with patch(...)` block that mocks requests,
    otherwise the worker can outlive the patch and hit the real network.
    """
    deadline = time.monotonic() + timeout
    while module._refresh_pending:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{module.__name__}: background refresh did not finish in {timeout}s")
        time.sleep(0.001)
    with module._refresh_lock:
        pass


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
