"""set_volume: volume 0 tears the stream down (no silent streaming); un-muting
from 0 resumes (gated like start(): enabled + quiet-override); an explicit
start() floors volume so it can't begin silent.

Built on a bare manager (like test_atc_discovery) so no threads/backends spawn.
"""
import contextlib
import threading
from unittest.mock import patch

from utilities.atc_audio import ATCAudioManager


def _mgr(playing=True, volume=70, mode="manual", output="usb", enabled=True):
    m = ATCAudioManager.__new__(ATCAudioManager)
    m._lock = threading.RLock()
    m._volume = volume
    m._playing = playing
    m._mode = mode
    m._output = output
    m._enabled = enabled
    m._quiet_override = False
    m._quiet = ("22:00", "06:00")
    m._cast_device = None
    m._mpv_proc = object() if (playing and output == "usb") else None
    return m


@contextlib.contextmanager
def _patched(m, in_quiet=False):
    with patch.object(m, "_start_backend_locked") as start, \
         patch.object(m, "_stop_backend_locked") as stop, \
         patch.object(m, "_mpv_set_volume") as setv, \
         patch.object(m, "_persist"), \
         patch.object(m, "_in_quiet_hours", return_value=in_quiet), \
         patch.object(m, "status", return_value={}):
        yield start, stop, setv


# ── volume 0 == off ──────────────────────────────────────────────────────────

def test_volume_zero_tears_down_backend_and_stops_playing():
    m = _mgr(playing=True, volume=70, output="usb")
    with _patched(m) as (start, stop, _):
        m.set_volume(0)
    assert m._volume == 0
    assert m._playing is False          # HomeKit now reads off
    stop.assert_called_once()           # backend torn down (not just muted)
    start.assert_not_called()


def test_volume_zero_when_not_playing_is_a_noop():
    m = _mgr(playing=False, volume=0, output="usb")
    with _patched(m) as (start, stop, _):
        m.set_volume(0)
    assert m._playing is False
    stop.assert_not_called()


# ── un-muting (0 -> N) resumes ───────────────────────────────────────────────

def test_unmute_from_zero_resumes_playback():
    m = _mgr(playing=False, volume=0, mode="manual", output="usb")
    with _patched(m) as (start, stop, _):
        m.set_volume(50)
    assert m._volume == 50
    assert m._playing is True
    start.assert_called_once()
    stop.assert_not_called()


def test_unmute_during_quiet_sets_override():
    m = _mgr(playing=False, volume=0, mode="auto", output="usb")
    with _patched(m, in_quiet=True) as (start, _, __):
        m.set_volume(50)
    assert m._playing is True
    assert m._quiet_override is True     # like start(): explicit gesture overrides
    start.assert_called_once()


# ── the two review findings: resume must NOT fire otherwise ───────────────────

def test_volume_nudge_while_stopped_does_not_resume():
    """A volume change from a NON-zero level (stopped by the quiet gate) must not
    start audio — else it plays at 2am and flaps off on the next tick."""
    m = _mgr(playing=False, volume=70, mode="auto", output="usb")
    with _patched(m, in_quiet=True) as (start, _, __):
        m.set_volume(60)                # 70 -> 60, not a 0 -> N unmute
    assert m._playing is False          # stayed stopped
    start.assert_not_called()


def test_unmute_while_disabled_does_not_resume():
    """ATC_ENABLED False: raising volume must not spawn a backend the tick then
    tears down."""
    m = _mgr(playing=False, volume=0, mode="auto", output="usb", enabled=False)
    with _patched(m) as (start, _, __):
        m.set_volume(50)
    assert m._playing is False
    start.assert_not_called()


def test_raising_volume_while_mode_off_does_not_start():
    m = _mgr(playing=False, volume=0, mode="off", output="usb")
    with _patched(m) as (start, _, __):
        m.set_volume(50)
    assert m._playing is False
    start.assert_not_called()


def test_volume_change_while_playing_is_live_no_restart():
    m = _mgr(playing=True, volume=70, output="usb")   # mpv_proc present
    with _patched(m) as (start, stop, setv):
        m.set_volume(30)
    assert m._volume == 30 and m._playing is True
    setv.assert_called_once_with(30)    # live change, not a restart
    start.assert_not_called()
    stop.assert_not_called()


# ── explicit start() must not begin silent ───────────────────────────────────

def test_start_floors_volume_from_zero():
    m = _mgr(playing=False, volume=0, mode="manual", output="usb")
    m._last_on_mode = "manual"
    m._station = "kjfk_twr"
    m._airplay_needs_pairing = ""
    m._airplay_failed = ""
    with patch.object(m, "_refresh_config"), \
         patch.object(m, "_ensure_station_locked"), \
         patch.object(m, "_in_quiet_hours", return_value=False), \
         patch.object(m, "_start_backend_locked"), \
         patch.object(m, "_persist"), \
         patch.object(m, "status", return_value={}):
        m.start()
    assert m._volume == 10              # floored so an explicit "on" isn't silent
    assert m._playing is True
