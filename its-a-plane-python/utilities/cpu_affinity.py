"""
cpu_affinity.py — keep background work off the LED refresh core.

WHY THIS EXISTS

`its-a-plane.py` pins the process to the isolated core (`isolcpus=3` on the boot
cmdline, then `os.sched_setaffinity(0, {3})`) so the rgbmatrix C refresh thread
drives the panel uninterrupted. That part is correct and deliberate.

The problem is that `sched_setaffinity(0, ...)` applies to the whole process, and
threads inherit it — so every background worker also lands on core 3, where it
competes with the hardware refresh thread for the one CPU nothing else may use.
When a worker burns CPU there, the refresh thread is starved, `SwapOnVSync()`
blocks waiting for a refresh that isn't finishing, and the scroll visibly freezes.

Measured on ernie (Pi 3A+, 64x32 panel), instrumenting per-keyframe frame time:

    before:  every FR24 poll cycle produced a 140-190ms frame (5 of 5 observed),
             showing up as `sync=142ms` / `plane_details=139ms`
    after:   0 of 7 cycles produced any slow frame

It stalls even on FR24 *cache hits*, which is what rules out network I/O and points
at CPU contention. Lowering `sys.setswitchinterval()` to 1ms changed nothing, which
rules out GIL hand-off latency too — the contention is with a C thread, not Python.

USAGE — call once at the top of any background worker that does real work:

    from utilities.cpu_affinity import run_off_render_core
    def _background_fetch(...):
        run_off_render_core()
        ...

Cheap (one syscall), never raises, and a no-op on machines that aren't pinned or
don't support affinity (macOS, containers) — so the test suite and the display
harness are unaffected.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Captured at import time, on the MAIN thread, i.e. whatever its-a-plane.py pinned
# the process to. Doing it here rather than hardcoding {3} keeps this correct if the
# render core ever changes or the pinning is removed entirely.
try:
    _RENDER_CORES = set(os.sched_getaffinity(0))
except (AttributeError, OSError):      # not Linux / not permitted
    _RENDER_CORES = set()

try:
    _ALL_CORES = set(range(os.cpu_count() or 1))
except Exception:
    _ALL_CORES = set()

# Everything except the render core. Deliberately EMPTY (a no-op) when:
#   * affinity is unreadable (macOS/dev boxes) — we don't know the render core, so
#     moving threads around would be guessing;
#   * the process isn't pinned (render == all cores) — nothing to move away from;
#   * single-core — nowhere to go.
#   * the captured mask is more than one core — under `isolcpus=3` the DEFAULT mask
#     is {0,1,2}, so if this module were ever imported BEFORE its-a-plane.py pins the
#     process, we would capture {0,1,2} and compute _GENERAL_CORES = {3}, moving every
#     worker ONTO the render core: the exact inverse of the intent, silent, and
#     invisible to the tests and harness (dev boxes take the no-op path). A real
#     render pin is exactly one core, so anything else means "don't touch affinity".
if (not _RENDER_CORES or not _ALL_CORES
        or _RENDER_CORES == _ALL_CORES or len(_RENDER_CORES) != 1):
    _GENERAL_CORES = set()
else:
    _GENERAL_CORES = _ALL_CORES - _RENDER_CORES

_warned = False


def run_off_render_core():
    """Move the CALLING THREAD to the non-render cores. Safe to call anywhere.

    `sched_setaffinity(0, ...)` sets the calling *thread* on Linux, so this moves
    only this worker — the render thread and the rgbmatrix refresh thread keep the
    isolated core to themselves.
    """
    global _warned
    if not _GENERAL_CORES:
        return False                    # not pinned, or nowhere else to go
    try:
        os.sched_setaffinity(0, _GENERAL_CORES)
        return True
    except (AttributeError, OSError) as e:
        if not _warned:                 # log once, never per call
            _warned = True
            logger.debug("cpu_affinity: could not move thread off render core: %s", e)
        return False


def describe():
    """For diagnostics/tests: what this module decided at import."""
    return {"render_cores": sorted(_RENDER_CORES),
            "general_cores": sorted(_GENERAL_CORES),
            "all_cores": sorted(_ALL_CORES)}
