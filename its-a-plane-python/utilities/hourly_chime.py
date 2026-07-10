"""
Hourly cabin chime — plays a short local "ding-dong" wav on the hour.

Always plays through the Pi's LOCAL output (USB speaker if attached, else the
onboard jack), independent of the ATC audio routing (browser/Chromecast/
AirPlay). Casting a 2-second sound would add multi-second connect latency, so
the chime is deliberately local-only.
"""
import logging
import os
import subprocess

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
        # problem in that import chain can never crash the display — this
        # module is imported by display/__init__ at load time.
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
    except FileNotFoundError:
        logger.warning("Hourly chime: mpv not installed — skipping")
    except Exception as e:
        logger.warning(f"Hourly chime: failed to play ({e})")
