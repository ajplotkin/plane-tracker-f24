"""
Hourly cabin chime — plays a short "ding-dong" wav on the hour through the
Pi's local speaker (USB audio card if present, else the onboard jack).

Fired by a systemd timer (see setup/systemd/) that calls fire_once() on the
hour. It must NOT be fired from inside the long-running tracker process: an mpv
fork()ed from the tracker fails ALSA card enumeration ("cannot get card index"),
even when the same command plays fine from a shell. A timer-fired chime runs in
a clean PID1-spawned service where the device opens normally. fire_once()
re-reads config each time, so the enable toggle, volume, and quiet-hours window
take effect with no restart.

Local-only by design: casting a 2-second sound over Chromecast/AirPlay would
add multi-second connect latency.
"""
import logging
import os
import re
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHIME_FILE = os.path.join(_BASE_DIR, "data", "ding_dong.wav")


def _usb_card_index():
    """ALSA card index (int) of the first USB-audio card, or None.

    The scheduler-fired chime intermittently fails with ALSA "cannot get card
    index for <name>" when a device is addressed by NAME (hw:CARD=UACDemoV10),
    even though the card is present. Addressing it by INDEX (hw:1) skips that
    name→index lookup, so we detect the index here for a fallback device.
    """
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                # " 1 [UACDemoV10     ]: USB-Audio - UACDemoV1.0"
                m = re.match(r"\s*(\d+)\s*\[[^\]]*\]:\s*(.+)$", line)
                if m and ("USB-Audio" in m.group(2) or "USB Audio" in m.group(2)):
                    return int(m.group(1))
    except OSError:
        pass
    return None


def _run_mpv(args):
    """Play once. Returns (returncode, stderr_text). Waits for the clip, reaps.

    mpv is spawned directly here. IMPORTANT: this must NOT run as a fork of the
    long-running tracker process — an mpv fork()ed from the tracker fails ALSA
    card enumeration ("cannot get card index for <card>") on every attempt, even
    though the identical command works from a shell, from systemd-run, and inside
    the tracker's own cgroup (ruled out: env, cgroup device policy, mlockall,
    affinity). The chime is therefore fired by an EXTERNAL systemd timer
    (fire_once()), so mpv runs in a clean PID1-spawned service, not a tracker
    fork. The in-process scheduler remains for setups without the timer.

    Uses communicate() (not wait()) so a chatty stderr can't fill the pipe and
    deadlock, and so we capture mpv's actual error on failure.
    """
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _, err = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()   # reap + drain the pipe
    return proc.returncode, (err.decode("utf-8", "replace").strip() if err else "")


def play(volume: int = 50):
    """Play the chime and report whether it was actually audible. Never raises.

    :param volume: mpv volume 0-100 (the wav is normalised, so this is the
                   effective loudness knob).
    """
    try:
        # Lazy import: keeps atc_audio (and its optional pychromecast/pyatv
        # deps) out of the display process at startup, and guarantees a
        # problem in that import chain can never crash the display.
        from utilities.atc_audio import ATCAudioManager
        primary = ATCAudioManager._detect_usb_alsa_device()  # 'alsa/usbmix' etc.
        idx = _usb_card_index()

        # Try device candidates best-first, and VERIFY each actually played
        # (mpv exits non-zero when the device can't open). The scheduler-fired
        # chime has been failing with ALSA "cannot get card index for <name>"
        # on the muxed device, so fall back to the card BY INDEX (skips the
        # name lookup) and finally mpv's default (onboard) — first that plays
        # wins. This also rides out a transient device blip.
        #   1. usbmix (dmix) — mixes over ATC (keeps them muxed)
        #   2. plughw:<index> — bypasses the failing name→index lookup
        #   3. default — onboard jack, last resort
        candidates = []
        for d in (primary, (f"alsa/plughw:{idx}" if idx is not None else None)):
            if d and d not in candidates:
                candidates.append(d)
        candidates.append(None)   # mpv default (onboard)

        errors = []
        for device in candidates:
            # --msg-level=all=error (not --really-quiet) so mpv prints the real
            # reason to stderr when the device won't open. Quiet on success.
            args = ["mpv", "--no-video", "--no-terminal", "--msg-level=all=error",
                    f"--volume={int(volume)}"]
            if device:
                args.append(f"--audio-device={device}")
            args.append(_CHIME_FILE)
            rc, err = _run_mpv(args)
            if rc == 0:
                logger.info("Hourly chime: rang (volume %s, device %s)",
                            int(volume), device or "default")
                return
            errors.append(f"{device or 'default'}: rc={rc} {err or ''}".strip())

        logger.warning("Hourly chime: NO SOUND — every output failed. %s",
                       " | ".join(errors))
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


def fire_once():
    """Play the chime now if enabled and not in quiet hours. Reads config fresh.
    Never raises. This is the entry point for the EXTERNAL systemd-timer
    scheduler (so mpv runs in a clean PID1 service, not a tracker fork) and is
    also called by the in-process scheduler below.
    """
    try:
        import config as cfg
        try:
            cfg.reload()
        except Exception:
            pass
        if not getattr(cfg, "HOURLY_CHIME_ENABLED", False):
            return
        if _in_quiet_hours(getattr(cfg, "HOURLY_CHIME_QUIET_START", ""),
                           getattr(cfg, "HOURLY_CHIME_QUIET_END", "")):
            logger.info("Hourly chime: quiet hours — skipped")
            return
        play(getattr(cfg, "HOURLY_CHIME_VOLUME", 50))
    except Exception as e:
        logger.warning(f"Hourly chime fire error: {e}")
