"""
Hourly cabin chime — plays a short "ding-dong" wav on the hour through the
Pi's LOCAL output (USB speaker if attached, else the onboard jack).

A dedicated daemon thread sleeps until the top of the next hour and fires
there (accurate to a fraction of a second — no frame-loop polling), so the
chime lands on :00, not up to 20s late. It re-reads config each hour, so the
enable toggle, volume, and quiet-hours window take effect on the next hour
without a restart.

Local-only by design: casting a 2-second sound over Chromecast/AirPlay would
add multi-second connect latency.
"""
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHIME_FILE = os.path.join(_BASE_DIR, "data", "ding_dong.wav")


def play(volume: int = 50):
    """Fire-and-forget local playback of the chime file. Never raises.

    :param volume: mpv volume 0-100 (the wav is normalised, so this is the
                   effective loudness knob).
    """
    try:
        # Lazy import: keeps atc_audio (and its optional pychromecast/pyatv
        # deps) out of the display process at startup, and guarantees a
        # problem in that import chain can never crash the display.
        from utilities.atc_audio import ATCAudioManager
        device = ATCAudioManager._detect_usb_alsa_device()

        args = ["mpv", "--no-video", "--no-terminal", "--really-quiet",
                f"--volume={int(volume)}"]
        if device:
            # Target the USB speaker explicitly; otherwise mpv plays to the
            # ALSA default (onboard jack) and the USB output stays silent.
            args.append(f"--audio-device={device}")
        args.append(_CHIME_FILE)

        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Pin to core 2 (leave core 3 for the LED refresh) — same as the ATC
        # mpv backend, so a chime can't micro-stutter the display.
        try:
            os.sched_setaffinity(proc.pid, {2})
        except Exception:
            pass
        # VERIFY it actually played — don't claim a ring just because Popen
        # succeeded. The USB speaker is an EXCLUSIVE plughw device: if ATC (or
        # anything) is already using it, mpv exits NON-ZERO with "Could not
        # open/initialize audio device -> no sound" and nothing is audible.
        # Wait for the (~2s) clip and check the exit code — deterministic,
        # unlike a fixed-delay poll. (This is the bug that made the log say
        # "rang" while the room was silent.)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()  # reap the SIGKILLed mpv — else returncode stays None
                         # (kill() doesn't set it) and it lingers as a <defunct>
                         # zombie until the next hour's Popen happens to reap it.
        if proc.returncode == 0:
            logger.info("Hourly chime: rang (volume %s, device %s)",
                        int(volume), device or "default")
        else:
            logger.warning(
                "Hourly chime: NO SOUND — mpv exited rc=%s; the USB speaker is "
                "busy (ATC or another stream is holding the exclusive device).",
                proc.returncode)
    except FileNotFoundError:
        logger.warning("Hourly chime: mpv not installed — skipping")
    except Exception as e:
        logger.warning(f"Hourly chime: failed to play ({e})")


# ── Scheduler ────────────────────────────────────────────────────────────

def _parse_hhmm(s):
    """'HH:MM' -> minutes since midnight, or None if unparseable/blank."""
    try:
        h, m = str(s).strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _in_quiet_hours(start_s, end_s, now=None):
    """True if `now` falls in [start, end). Handles overnight windows
    (22:00-08:00). Blank or equal start/end => never quiet."""
    a, b = _parse_hhmm(start_s), _parse_hhmm(end_s)
    if a is None or b is None or a == b:
        return False
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    return (a <= cur < b) if a < b else (cur >= a or cur < b)


def _seconds_to_next_hour(now=None):
    now = now or datetime.now()
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())


def _run_scheduler():
    while True:
        # +0.5s margin so we always wake just PAST the boundary (never a hair
        # before, which could double-fire).
        time.sleep(_seconds_to_next_hour() + 0.5)
        try:
            import config as cfg
            # Re-read config.json from disk so a web-UI save (which reloads in
            # the SEPARATE web process) is picked up here next hour — no
            # display restart needed. Safe: display scenes hold their own
            # captured copies, so this only refreshes what the scheduler reads.
            try:
                cfg.reload()
            except Exception:
                pass
            if not getattr(cfg, "HOURLY_CHIME_ENABLED", False):
                continue
            if _in_quiet_hours(getattr(cfg, "HOURLY_CHIME_QUIET_START", ""),
                               getattr(cfg, "HOURLY_CHIME_QUIET_END", "")):
                logger.info("Hourly chime: quiet hours — skipped")
                continue
            play(getattr(cfg, "HOURLY_CHIME_VOLUME", 50))
        except Exception as e:
            logger.warning(f"Hourly chime scheduler error: {e}")


_scheduler_started = False


def start_scheduler():
    """Start the hourly scheduler thread once. Safe to call repeatedly."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_run_scheduler, daemon=True,
                     name="hourly-chime").start()
    logger.info("Hourly chime scheduler started")
