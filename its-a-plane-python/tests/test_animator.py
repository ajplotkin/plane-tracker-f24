"""The render loop's frame arithmetic and its frame-budget telemetry.

Neither is reachable from testing/e2e_debug.py, which REPLICATES play() rather
than calling it — so these drive the real play() under a fake clock.
"""

import logging
from unittest.mock import patch

import pytest

from utilities import animator as anim
from utilities.animator import Animator


class _Stop(BaseException):
    """Escapes play(): keyframes are isolated with `except Exception`."""


class TestKeyFrameDivisor:

    def test_a_fractional_divisor_is_refused(self):
        """At 15 fps, PER_SECOND * 0.5 is 7.5, and frame % 7.5 == 0 only every
        15 frames — a half-second keyframe silently runs once a second."""
        with pytest.raises(ValueError):
            Animator.KeyFrame.add(7.5)

    def test_float_noise_is_rounded_to_the_whole_frame(self):
        """1 / (some PERIOD) can come out as 15.000000000000002."""
        @Animator.KeyFrame.add(15.000000000000002)
        def f(self, count):
            pass
        assert f.properties["divisor"] == 15
        assert isinstance(f.properties["divisor"], int)

    def test_zero_still_means_run_once(self):
        @Animator.KeyFrame.add(0)
        def f(self, count):
            pass
        assert f.properties["divisor"] == 0

    def test_a_whole_float_divisor_fires_on_schedule(self):
        """frame % 15.0 is exact, but an int divisor removes the question."""
        @Animator.KeyFrame.add(15.0)
        def f(self, count):
            pass
        assert f.properties["divisor"] == 15


class _Clock:
    """monotonic()/sleep() that advance only when the loop spends time."""

    def __init__(self, overshoot=0.0):
        self.t = 1000.0
        self.overshoot = overshoot

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.t += dt + self.overshoot


def _run(work_s, frames, overshoot=0.0, delay=1 / 15):
    """Play `frames` frames, each spending `work_s` (callable or float)."""
    clock = _Clock(overshoot)

    class Scene(Animator):
        @Animator.KeyFrame.add(1)
        def tick(self, count):
            w = work_s(self.frame) if callable(work_s) else work_s
            clock.t += w
            if self.frame >= frames:
                raise _Stop

    with patch.object(anim, "monotonic", clock.monotonic), \
         patch.object(anim, "sleep", clock.sleep):
        s = Scene()
        s.delay = delay
        with pytest.raises(_Stop):
            s.play()
    return s


class TestFrameBudgetTelemetry:

    def test_a_healthy_panel_logs_nothing(self, caplog):
        caplog.set_level(logging.INFO, logger=anim.__name__)
        _run(0.005, frames=15 * 125)            # 5 ms of work, two minutes
        assert not [r for r in caplog.records if "deadline" in r.message]

    def test_slow_scene_work_is_reported_as_work(self, caplog):
        caplog.set_level(logging.INFO, logger=anim.__name__)
        # one 80 ms frame every ~6.7 s against a 67 ms budget
        _run(lambda f: 0.080 if f % 100 == 0 else 0.005, frames=15 * 65)
        msgs = [r.message for r in caplog.records if "deadline" in r.message]
        assert msgs, "a missed deadline was not reported"
        assert "worst work 80 ms" in msgs[0], msgs[0]

    def test_a_late_start_is_told_apart_from_slow_work(self, caplog):
        """A frame that begins late because sleep overshot (or a fetch thread
        held the GIL) is not a scene problem, and must not read as one."""
        caplog.set_level(logging.INFO, logger=anim.__name__)
        _run(0.002, frames=15 * 65, overshoot=0.070)
        msgs = [r.message for r in caplog.records if "deadline" in r.message]
        assert msgs, "late starts were not reported"
        assert "worst work 2 ms" in msgs[0], msgs[0]
        assert "worst late start 70 ms" in msgs[0], msgs[0]

    def test_it_logs_at_most_once_a_minute(self, caplog):
        caplog.set_level(logging.INFO, logger=anim.__name__)
        _run(0.080, frames=15 * 65)            # EVERY frame over, for >1 min
        msgs = [r for r in caplog.records if "deadline" in r.message]
        # 65 frames x 80 ms ≈ 78 s of virtual time: exactly one report
        assert len(msgs) == 1, [r.message for r in msgs]

    def test_the_counters_reset_after_each_report(self):
        s = _run(lambda f: 0.080 if f < 10 else 0.005, frames=15 * 65)
        assert s._frame_budget["over"] == 0, (
            "overruns from before the report carried into the next minute")


def test_the_mirror_is_told_the_panels_scroll_rate(tmp_path, monkeypatch):
    """The panel scrolls one pixel per frame. The mirror used to assume 10 px/s
    in two places, so changing setup/frames.py desynced it without any failure.
    It now reads the rate from the server, which must report the real one."""
    monkeypatch.setenv("PLANE_TRACKER_DATA_DIR", str(tmp_path))
    import web.app as app_mod
    from setup import frames
    d = app_mod.app.test_client().get("/api/display-state").get_json()
    assert d["scroll_px_per_sec"] == frames.PER_SECOND


class TestPanelFpsSetting:
    """PANEL_FPS in config/config.json sets the frame rate (and so the scroll
    speed) per device. A bad value must fall back, never stop the panel: it can
    arrive through an unauthenticated web form onto a Pi nobody can reach."""

    def _fps(self, tmp_path, value=..., raw=None):
        import json
        from setup import frames
        f = tmp_path / "config.json"
        if raw is not None:
            f.write_text(raw, encoding="utf-8")
        elif value is not ...:
            f.write_text(json.dumps({"PANEL_FPS": value}), encoding="utf-8")
        return frames._configured_fps(str(f))

    def test_no_file_means_ten(self, tmp_path):
        assert self._fps(tmp_path) == 10

    def test_unset_means_ten(self, tmp_path):
        assert self._fps(tmp_path, raw='{"BRIGHTNESS": 50}') == 10

    def test_a_whole_number_is_used(self, tmp_path):
        assert self._fps(tmp_path, 15) == 15
        assert self._fps(tmp_path, "15") == 15     # web forms send strings

    @pytest.mark.parametrize("bad", [12.5, "12.5", 7, 21, 0, -5, "fast", [], {}])
    def test_bad_values_fall_back_to_ten(self, tmp_path, bad):
        """12.5 would make every PER_SECOND multiple fractional, and the
        animator refuses those at import — a stopped panel. Out of range is
        either uselessly slow or past the render budget."""
        assert self._fps(tmp_path, bad) == 10

    def test_a_corrupt_file_falls_back(self, tmp_path):
        assert self._fps(tmp_path, raw="{not json") == 10

    def test_every_keyframe_divisor_is_whole_at_every_allowed_rate(self):
        """The whole point of whole-number rates: every PER_SECOND multiple the
        codebase uses must land on a whole frame at 8..20 fps."""
        import re, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        mults = set()
        for py in list(root.glob("scenes/*.py")) + list(root.glob("display/*.py")):
            for m in re.finditer(r"PER_SECOND\s*\*\s*([0-9.]+)", py.read_text()):
                mults.add(float(m.group(1)))
        assert mults, "found no PER_SECOND multiples — the scan is broken"
        for fps in range(8, 21):
            for k in mults:
                d = fps * k
                if k == 0.2:          # loading pulse rounds by design
                    continue
                assert d == int(d), f"PER_SECOND*{k} = {d} at {fps} fps"
