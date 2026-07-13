"""Hourly chime playback: honest reporting + device fallback on failure.

Candidates are tried best-first: the muxed usbmix device, then the USB card by
INDEX (bypasses the ALSA name→index lookup that fails in the scheduler context),
then mpv's default. First that actually plays wins.
"""
import logging
from unittest.mock import patch

from utilities import hourly_chime

_DEV = "utilities.atc_audio.ATCAudioManager._detect_usb_alsa_device"
_IDX = "utilities.hourly_chime._usb_card_index"


def test_first_device_rings_no_fallback():
    with patch("utilities.hourly_chime._run_mpv", return_value=(0, "")) as m, \
            patch(_DEV, return_value="alsa/usbmix"), patch(_IDX, return_value=1):
        hourly_chime.play(50)
    assert m.call_count == 1   # usbmix rang; no fallback attempted


def test_falls_back_to_index_device_when_usbmix_name_lookup_fails(caplog):
    with patch("utilities.hourly_chime._run_mpv",
               side_effect=[(2, "cannot get card index for UACDemoV10"), (0, "")]) as m, \
            patch(_DEV, return_value="alsa/usbmix"), patch(_IDX, return_value=1), \
            caplog.at_level(logging.INFO):
        hourly_chime.play(50)
    assert m.call_count == 2
    assert any("plughw:1" in r.message for r in caplog.records)  # rang on index device


def test_all_outputs_fail_logs_real_errors_not_an_atc_guess(caplog):
    real_err = "[ao/alsa] Playback open error: Device or resource busy"
    with patch("utilities.hourly_chime._run_mpv",
               side_effect=[(2, real_err), (2, real_err), (2, real_err)]) as m, \
            patch(_DEV, return_value="alsa/usbmix"), patch(_IDX, return_value=1), \
            caplog.at_level(logging.WARNING):
        hourly_chime.play(50)
    assert m.call_count == 3   # usbmix, plughw:1, default all tried
    msg = " ".join(r.message for r in caplog.records)
    assert "Device or resource busy" in msg   # mpv's ACTUAL error surfaced
    assert "ATC" not in msg                    # no fabricated cause


def test_never_raises_when_mpv_missing():
    with patch("utilities.hourly_chime._run_mpv", side_effect=FileNotFoundError()), \
            patch(_DEV, return_value=""), patch(_IDX, return_value=None):
        hourly_chime.play(50)   # must not raise


# ── fire_once() — the external-timer entry point ─────────────────────────────

def _cfg(monkeypatch, enabled=True, vol=50, qstart="", qend=""):
    import config as cfg
    monkeypatch.setattr(cfg, "reload", lambda: None, raising=False)
    monkeypatch.setattr(cfg, "HOURLY_CHIME_ENABLED", enabled, raising=False)
    monkeypatch.setattr(cfg, "HOURLY_CHIME_VOLUME", vol, raising=False)
    monkeypatch.setattr(cfg, "HOURLY_CHIME_QUIET_START", qstart, raising=False)
    monkeypatch.setattr(cfg, "HOURLY_CHIME_QUIET_END", qend, raising=False)


def test_fire_once_plays_when_enabled(monkeypatch):
    _cfg(monkeypatch, enabled=True, vol=42)
    with patch("utilities.hourly_chime.play") as p:
        hourly_chime.fire_once()
    p.assert_called_once_with(42)


def test_fire_once_silent_when_disabled(monkeypatch):
    _cfg(monkeypatch, enabled=False)
    with patch("utilities.hourly_chime.play") as p:
        hourly_chime.fire_once()
    p.assert_not_called()


def test_fire_once_skips_in_quiet_hours(monkeypatch):
    _cfg(monkeypatch, enabled=True, qstart="00:00", qend="23:59")  # always quiet
    with patch("utilities.hourly_chime.play") as p:
        hourly_chime.fire_once()
    p.assert_not_called()


def test_internal_scheduler_disabled_when_external_flag_set(monkeypatch):
    monkeypatch.setenv("CHIME_EXTERNAL_SCHEDULER", "1")
    hourly_chime._scheduler_started = False
    with patch("utilities.hourly_chime.threading.Thread") as T:
        hourly_chime.start_scheduler()
    T.assert_not_called()   # no in-process thread; the systemd timer fires it
