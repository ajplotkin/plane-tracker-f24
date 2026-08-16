"""The scenes that read utilities/temperature.py, across the ASYNC arrival.

utilities/temperature.py no longer blocks: its getters return the last good
value (or None/[]) and a background worker fills them in a beat later. That
turns "no data" from a momentary state inside one blocking call into a state the
render loop can observe for several frames — so these tests pin the two things
that matters on a LIVE framebuffer (display/__init__.py sync() discards
SwapOnVSync's return, so there is no double buffer):

  1. no value -> None -> value FLAP. Once a scene has drawn a real value it must
     never fall back to the "no data" rendering (red ERR / red clock / red date)
     because a later fetch came back empty.
  2. no redundant repaints. A frame whose content did not change must write
     nothing at all.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch


# ── scenes/temperature.py ───────────────────────────────────────────────────

def _drawn(draw_text):
    """The text of every DrawText call that actually lights pixels: not black
    (that is an erase) and not empty (DrawText("") renders no glyphs)."""
    from setup import colours
    out = []
    for call in draw_text.call_args_list:
        colour, text = call.args[4], call.args[5]
        if colour is not colours.BLACK and text:
            out.append(text)
    return out


@contextmanager
def _temp_render(reading=(None, None)):
    """Drive TemperatureScene with a controllable reading.

    The UV and AQI chips share the temperature row and are fed by their own
    background fetchers, so they are pinned too — an AQI value landing mid-test
    would otherwise repaint the row and break these counts.

    `graphics` is patched ONCE for the whole test: it is a MagicMock, so
    `graphics.Color(...)` hands back one singleton per patch, and re-patching
    mid-test would make the scene's colour_key compare unequal across phases and
    fake a repaint. Yields (scene, graphics_mock, set_reading).
    """
    import scenes.temperature as st
    scene = st.TemperatureScene()
    scene.canvas = MagicMock()
    scene._data = []
    box = {"reading": reading}
    with patch.object(st, "grab_temperature_and_humidity",
                      side_effect=lambda: box["reading"]), \
            patch.object(st, "get_current_uv", return_value=None), \
            patch.object(st, "get_aqi", return_value=None), \
            patch.object(st, "graphics") as g:
        yield st, scene, g, box


def test_temperature_scene_shows_nothing_then_the_value_never_err_in_between():
    """A cold boot now has a gap between the first frame and the first reading.
    The panel must stay blank through it, not flash a red ERR."""
    with _temp_render() as (st, scene, g, box):
        for _ in range(5):                      # ~5 render frames, no data yet
            scene.temperature(0)
        assert _drawn(g.DrawText) == []         # nothing drawn at all

        box["reading"] = (71.6, 52.0)           # the worker lands
        scene.temperature(0)
        assert _drawn(g.DrawText) == ["72°"]
        g.DrawText.reset_mock()
        for _ in range(10):                     # ten unchanged frames...
            scene.temperature(0)
        assert g.DrawText.call_count == 0       # ...write nothing


def test_temperature_scene_falls_back_to_err_only_after_the_grace_period():
    with _temp_render() as (st, scene, g, box):
        scene._first_temp_attempt = datetime.now() - timedelta(
            seconds=st.ERR_GRACE_SECONDS + 1)
        scene.temperature(0)
        assert _drawn(g.DrawText) == ["ERR"]


def test_temperature_scene_never_flaps_back_to_err():
    """The getter returns (None, None) whenever a refresh fails. The scene keeps
    its last good pair, so a failing refresh must not repaint ERR."""
    with _temp_render(reading=(71.6, 52.0)) as (st, scene, g, box):
        scene.temperature(0)
        assert _drawn(g.DrawText) == ["72°"]
        g.DrawText.reset_mock()

        box["reading"] = (None, None)           # every later refresh fails
        for _ in range(20):
            scene._last_updated = datetime.now() - timedelta(
                seconds=st.TEMPERATURE_REFRESH_SECONDS + 1)   # always due
            scene.temperature(0)
        assert g.DrawText.call_count == 0       # not one pixel touched


def test_temperature_scene_picks_up_a_late_value_on_the_very_next_frame():
    """A refresh that fails must not arm a scene-level backoff. The getter is
    non-blocking and self-gated now, so the scene keeps asking and shows the
    value the frame after the background worker lands it — the old 60-second
    error backoff here would sit on a stale reading for a minute instead."""
    with _temp_render(reading=(71.6, 52.0)) as (st, scene, g, box):
        scene.temperature(0)
        g.DrawText.reset_mock()

        scene._last_updated = datetime.now() - timedelta(
            seconds=st.TEMPERATURE_REFRESH_SECONDS + 1)   # refresh is due
        box["reading"] = (None, None)
        scene.temperature(0)                              # ...and it fails
        assert g.DrawText.call_count == 0                 # nothing repainted

        box["reading"] = (75.4, 52.0)                     # the worker lands
        scene.temperature(0)                              # the VERY next frame
        assert _drawn(g.DrawText) == ["75°"]


def test_temperature_scene_repaints_exactly_once_when_the_value_changes():
    with _temp_render(reading=(71.6, 52.0)) as (st, scene, g, box):
        scene.temperature(0)
        g.DrawText.reset_mock()

        box["reading"] = (75.4, 52.0)
        scene._last_updated = datetime.now() - timedelta(
            seconds=st.TEMPERATURE_REFRESH_SECONDS + 1)
        for _ in range(6):
            scene.temperature(0)
        # exactly one black erase of the old string + one draw of the new one
        assert g.DrawText.call_count == 2
        assert _drawn(g.DrawText) == ["75°"]


# ── scenes/daysforecast.py ──────────────────────────────────────────────────

def _forecast_payload(icon="10000", tmin=60.0, tmax=80.0):
    base = datetime.now().astimezone().replace(microsecond=0)
    return [{
        "startTime": (base + timedelta(days=i)).isoformat(),
        "values": {"temperatureMin": tmin, "temperatureMax": tmax,
                   "weatherCodeFullDay": icon},
    } for i in range(3)]


def _fc_scene():
    import scenes.daysforecast as ds
    scene = ds.DaysForecastScene()
    scene.canvas = MagicMock()
    scene._data = []
    scene.overhead = MagicMock()
    scene.overhead.tracked_data = None
    scene.draw_square = MagicMock()
    scene._cached_forecast = None          # ignore any real disk cache
    scene._iss_active = False
    return ds, scene


def test_forecast_scene_draws_nothing_until_data_lands_then_once():
    ds, scene = _fc_scene()
    with patch.object(ds, "grab_forecast", return_value=[]) as gf, \
            patch.object(ds, "graphics") as g:
        for _ in range(5):
            scene.day(0)
        assert g.DrawText.call_count == 0          # canvas untouched
        assert scene.draw_square.call_count == 0   # and never pre-cleared
        # asks on EVERY frame while empty (the getter is free now), so the
        # forecast appears as soon as the worker lands it
        assert gf.call_count == 5

    with patch.object(ds, "grab_forecast", return_value=_forecast_payload()), \
            patch.object(ds, "_icon_pixels", return_value=None), \
            patch.object(ds, "graphics") as g:
        scene.day(0)
        assert g.DrawText.call_count == 9          # 3 days x (day, max, min)
        g.DrawText.reset_mock()
        scene.draw_square.reset_mock()
        for _ in range(10):
            scene.day(0)
        assert g.DrawText.call_count == 0          # unchanged -> no repaint
        assert scene.draw_square.call_count == 0


def test_forecast_scene_keeps_the_last_good_list_when_a_refresh_returns_empty():
    ds, scene = _fc_scene()
    with patch.object(ds, "grab_forecast", return_value=_forecast_payload()), \
            patch.object(ds, "_icon_pixels", return_value=None), \
            patch.object(ds, "graphics"):
        scene.day(0)
    scene._last_hour = (datetime.now().hour + 1) % 24     # force a refresh
    scene.draw_square.reset_mock()
    with patch.object(ds, "grab_forecast", return_value=[]), \
            patch.object(ds, "_icon_pixels", return_value=None), \
            patch.object(ds, "graphics") as g:
        for _ in range(5):
            scene.day(0)
        assert g.DrawText.call_count == 0
        assert scene.draw_square.call_count == 0
    assert scene._cached_forecast                          # last good retained


# ── scenes/clock.py ─────────────────────────────────────────────────────────

def test_clock_sunrise_sunset_never_flaps_back_to_none():
    """None turns the clock RED. Once the times are known, an empty or raising
    forecast must return the last known pair instead."""
    import scenes.clock as ck
    scene = ck.ClockScene.__new__(ck.ClockScene)
    scene.today_sunrise = datetime(2026, 8, 16, 10, 0)
    scene.today_sunset = datetime(2026, 8, 16, 23, 0)
    scene.last_fetch_date = None            # force the refresh branch
    scene._forecast_retry_after = 0

    with patch.object(ck, "grab_forecast", return_value=[]) as gf:
        assert scene.calculate_sunrise_sunset() == (scene.today_sunrise,
                                                    scene.today_sunset)
        gf.assert_called_once()

    # the empty forecast above armed the 5-min gate; clear it so the raising
    # getter is actually reached (otherwise this asserts nothing)
    scene._forecast_retry_after = 0
    with patch.object(ck, "grab_forecast", side_effect=RuntimeError("boom")) as gf:
        assert scene.calculate_sunrise_sunset() == (scene.today_sunrise,
                                                    scene.today_sunset)
        gf.assert_called_once()


def test_clock_retries_every_frame_while_it_has_no_sun_times_at_all():
    """With nothing cached the clock is RED, so it must not sit out the 5-minute
    failure gate after the background fetch has already landed."""
    import scenes.clock as ck
    scene = ck.ClockScene.__new__(ck.ClockScene)
    scene.today_sunrise = None
    scene.today_sunset = None
    scene.last_fetch_date = None
    scene._forecast_retry_after = 0

    with patch.object(ck, "grab_forecast", return_value=[]) as gf:
        for _ in range(5):
            assert scene.calculate_sunrise_sunset() == (None, None)
        assert gf.call_count == 5           # kept asking

    # ...and the gate DOES apply once there is something to fall back on
    scene.today_sunrise = datetime(2026, 8, 16, 10, 0)
    scene.today_sunset = datetime(2026, 8, 16, 23, 0)
    scene._forecast_retry_after = 0        # the 5 failures above armed the gate
    with patch.object(ck, "grab_forecast", return_value=[]) as gf:
        for _ in range(5):
            scene.calculate_sunrise_sunset()
        assert gf.call_count == 1           # gate held after the first failure


# ── scenes/date.py ──────────────────────────────────────────────────────────

def test_date_moonphase_never_flaps_back_to_none():
    """None makes the date red; the moon phase must only ever move forward."""
    import scenes.date as dt
    scene = dt.DateScene.__new__(dt.DateScene)
    scene.today_moonphase = None
    scene.last_fetched_moonphase = None

    with patch.object(dt, "grab_forecast", return_value=[]):
        assert scene.moonphase() is None                  # cold: red date

    good = [{"startTime": datetime.now().strftime("%Y-%m-%dT00:00:00"),
             "values": {"moonPhase": 3}}]
    with patch.object(dt, "grab_forecast", return_value=good):
        assert scene.moonphase() == 3

    scene.last_fetched_moonphase = None                   # force a re-fetch
    with patch.object(dt, "grab_forecast", return_value=[]):
        assert scene.moonphase() == 3                     # keeps the last good
    with patch.object(dt, "grab_forecast", side_effect=RuntimeError("boom")):
        assert scene.moonphase() == 3
