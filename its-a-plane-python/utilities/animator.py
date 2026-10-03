import gc
import logging
from time import sleep, monotonic, strftime, localtime, time

_log = logging.getLogger(__name__)


# Garbage-collector pause telemetry. A collection holds the GIL for its whole
# duration, so a slow one in ANY thread delays the render thread's next frame —
# it shows in the frame telemetry as a late start with little work, which is
# what an unexplained ~200 ms hitch looked like on 2026-10-03. Counting slow
# collections here lets the per-minute report say whether GC was the cause.
_GC_SLOW_S = 0.05
_gc_stats = {"slow": 0, "worst": 0.0, "worst_gen": -1, "t0": 0.0}


def _gc_callback(phase, info):
    # Runs inside the collector: keep it to arithmetic, no allocation-heavy work.
    if phase == "start":
        _gc_stats["t0"] = monotonic()
        return
    d = monotonic() - _gc_stats["t0"]
    if d > _GC_SLOW_S:
        _gc_stats["slow"] += 1
        if d > _gc_stats["worst"]:
            _gc_stats["worst"] = d
            _gc_stats["worst_gen"] = info.get("generation", -1)


if _gc_callback not in gc.callbacks:
    gc.callbacks.append(_gc_callback)

DELAY_DEFAULT = 0.01


class Animator(object):
    class KeyFrame(object):
        @staticmethod
        def add(divisor, offset=0):
            # The play loop fires a keyframe when `frame % divisor == 0`. A
            # fractional divisor does not fail — it silently fires at a
            # different rate: at 15 fps, PER_SECOND * 0.5 is 7.5, and
            # frame % 7.5 is zero only every 15 frames, so a half-second
            # keyframe quietly becomes a one-second one. At 10 fps every
            # such product happened to be whole, which is why nothing caught
            # it. Round to the nearest frame, and refuse anything that was
            # not meant to be whole.
            if divisor:
                rounded = int(round(divisor))
                if abs(divisor - rounded) > 1e-6:
                    raise ValueError(
                        f"KeyFrame divisor {divisor!r} is not a whole number of "
                        f"frames at this frame rate")
                divisor = rounded

            def wrapper(func):
                func.properties = {"divisor": divisor, "offset": offset, "count": 0}
                return func

            return wrapper

    def __init__(self):
        self.keyframes = []
        self.frame = 0
        self._delay = DELAY_DEFAULT
        # Frame-budget telemetry, reset every minute; see play().
        self._frame_budget = {"n": 0, "over": 0, "worst_work": 0.0,
                              "worst_wake": 0.0, "worst_at": 0.0,
                              "worst_kf": "", "worst_kf_t": 0.0,
                              "t": monotonic()}

        self._register_keyframes()

        super().__init__()

    def _register_keyframes(self):
        # Some introspection to setup keyframes
        for methodname in dir(self):
            method = getattr(self, methodname)
            if hasattr(method, "properties"):
                self.keyframes.append(method)

    def reset_scene(self):
        for keyframe in self.keyframes:
            if keyframe.properties["divisor"] == 0:
                keyframe()

    def play(self):
        # Drift-corrected pacing: the old fixed sleep AFTER each frame's
        # work made the real period = delay + work, so the scroll rate
        # wobbled with scene workload and averaged below the nominal rate
        # (also desyncing the web mirror, which assumes one px per PERIOD).
        next_frame = monotonic()
        while True:
            t0 = monotonic()   # when this frame's work actually began
            _slow_kf, _slow_kf_t = None, 0.0
            for keyframe in self.keyframes:
                _kf_t0 = monotonic()
                # A single scene keyframe raising must NOT kill the animation
                # loop — that propagates out of play() and freezes the ENTIRE
                # panel on the last frame. Isolate each keyframe: log (throttled
                # per keyframe to avoid flooding at frame rate) and continue.
                try:
                    # If divisor == 0 then only run once on first loop
                    if self.frame == 0:
                        if keyframe.properties["divisor"] == 0:
                            keyframe()

                    # Otherwise perform normal operation
                    if (
                        self.frame > 0
                        and keyframe.properties["divisor"]
                        and not (
                            (self.frame - keyframe.properties["offset"])
                            % keyframe.properties["divisor"]
                        )
                    ):
                        if keyframe(keyframe.properties["count"]):
                            keyframe.properties["count"] = 0
                        else:
                            keyframe.properties["count"] += 1
                    # Which keyframe made a slow frame slow. Without this, the
                    # telemetry could say a frame did 250 ms of work but not
                    # whose work it was.
                    _kf_d = monotonic() - _kf_t0
                    if _kf_d > _slow_kf_t:
                        _slow_kf, _slow_kf_t = keyframe, _kf_d
                except Exception:
                    _name = getattr(keyframe, "__name__", repr(keyframe))
                    _errs = self.__dict__.setdefault("_keyframe_err_ts", {})
                    _t = monotonic()
                    if _t - _errs.get(_name, 0.0) > 60:
                        _errs[_name] = _t
                        _log.exception(
                            "Animator: keyframe %s raised (continuing)", _name)

            self.frame += 1
            next_frame += self._delay
            now = monotonic()

            # Frame-budget telemetry. A frame that misses its deadline is a
            # visible stall on the panel, and until this existed the loop
            # absorbed it silently — so the only way to judge a frame-rate
            # change was by eye. Logged at most once a minute, and only when a
            # deadline was missed, so a healthy panel logs nothing.
            #
            # Two numbers, because they have different causes and different
            # fixes. WORK is the keyframes themselves (scene drawing plus the
            # SwapOnVSync wait). WAKE is how late the frame began against its
            # schedule — sleep overshoot, or the GIL held by a fetch thread,
            # which is the FR24-poll stall cpu_affinity.py exists to prevent.
            # A miss whose wake is large and work small is not a scene problem.
            _fb = self._frame_budget
            _work = now - t0
            _wake = max(0.0, t0 - (next_frame - self._delay))
            _fb["n"] += 1
            if _work + _wake > self._delay:
                _fb["over"] += 1
                _fb["worst_work"] = max(_fb["worst_work"], _work)
                if _slow_kf is not None and _slow_kf_t > _fb["worst_kf_t"]:
                    _fb["worst_kf_t"] = _slow_kf_t
                    _fb["worst_kf"] = getattr(_slow_kf, "__name__", "?")
                if _wake > _fb["worst_wake"]:
                    _fb["worst_wake"] = _wake
                    # Wall-clock time of the worst stall, so it can be lined up
                    # against the journal (FR24 polls, fetches, restarts).
                    _fb["worst_at"] = time()
            if now - _fb["t"] >= 60:
                if _fb["over"]:
                    _gcs = ""
                    if _gc_stats["slow"]:
                        _gcs = (f"; {_gc_stats['slow']} GC pause(s) over "
                                f"{_GC_SLOW_S * 1000:.0f} ms, worst "
                                f"{_gc_stats['worst'] * 1000:.0f} ms "
                                f"(gen {_gc_stats['worst_gen']})")
                    _at = (strftime("%H:%M:%S", localtime(_fb["worst_at"]))
                           if _fb["worst_at"] else "-")
                    _kf = (f"; slowest keyframe {_fb['worst_kf']} "
                           f"{_fb['worst_kf_t'] * 1000:.0f} ms"
                           if _fb["worst_kf"] else "")
                    _log.info(
                        "Animator: %d/%d frames missed the %.0f ms deadline in "
                        "the last minute (worst work %.0f ms, worst late start "
                        "%.0f ms at %s%s%s)",
                        _fb["over"], _fb["n"], self._delay * 1000,
                        _fb["worst_work"] * 1000, _fb["worst_wake"] * 1000,
                        _at, _kf, _gcs)
                _fb.update(n=0, over=0, worst_work=0.0, worst_wake=0.0,
                           worst_at=0.0, worst_kf="", worst_kf_t=0.0, t=now)
                _gc_stats.update(slow=0, worst=0.0, worst_gen=-1)

            if next_frame < now:
                next_frame = now  # fell behind; don't burst to catch up
            else:
                sleep(next_frame - now)

    @property
    def delay(self):
        return self._delay

    @delay.setter
    def delay(self, value):
        self._delay = value


if __name__ == "__main__":

    class Test(Animator):
        @Animator.KeyFrame.add(5, 1)
        def method1(self, frame):
            print(f"method1 {frame}")

        @Animator.KeyFrame.add(1, 1)
        def method2(self, frame):
            print(f"method2 {frame}")

    myclass = Test()
    myclass.play()

    while 1:
        sleep(5)
