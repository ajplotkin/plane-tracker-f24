from utilities.animator import Animator
from utilities import textclip
from setup import colours, fonts, screen

from rgbmatrix import graphics

# Setup
FLIGHT_NO_DISTANCE_FROM_TOP = 24
FLIGHT_NO_TEXT_HEIGHT = 8  # based on font size
FLIGHT_NO_FONT = fonts.small
FLIGHT_NO_CLIP_FONT = textclip.small  # same 5x8.bdf, for boundary clipping

FLIGHT_NUMBER_ALPHA_COLOUR = colours.LIGHT_PURPLE
FLIGHT_NUMBER_NUMERIC_COLOUR = colours.LIGHT_ORANGE

DATA_INDEX_POSITION = (52, 24)
DATA_INDEX_FONT = fonts.extrasmall
DATA_INDEX_COLOUR = colours.GREY

# While an ISS pass is overhead but a flight page is showing (dwell
# rotation), the indicator zone doubles as a badge: "ISS" (4x6 = exactly
# 12px wide) alternates with the page indicator every 2 s; with a single
# flight it shows steadily. Steel blue matches the ISS alert colour.
ISS_BADGE_COLOUR = graphics.Color(100, 130, 180)
ISS_BADGE_PHASE_FRAMES = 20  # 2 s per face

# The canvas is live (sync() discards SwapOnVSync's return, so self.canvas
# IS the displayed framebuffer). Every write is visible immediately —
# flicker-free rendering requires that the indicator zone (x >= 52) is
# written at most once per state change, never per frame. Scroll text is
# therefore clipped at the zone edge instead of drawn through and stamped.


class FlightDetailsScene(object):
    def __init__(self):
        super().__init__()
        self._indicator_state = "reset"

    @Animator.KeyFrame.add(1)
    def flight_details(self, count):

        # Guard against no data or ISS takeover
        if len(self._data) == 0 or getattr(self, '_iss_active', False):
            # ISS scene owns the canvas; force an indicator redraw on resume
            self._indicator_state = "reset"
            return

        has_indicator = len(self._data) > 1
        iss_overhead = getattr(self, "_iss_pass_active", False)

        # Scroll text may use the full width when the zone is unoccupied;
        # with a page indicator OR the ISS badge there, it is clipped at
        # the zone edge so the zone is never touched by per-frame draws.
        zone_occupied = has_indicator or iss_overhead
        boundary = DATA_INDEX_POSITION[0] if zone_occupied else screen.WIDTH

        # 1. Clear scroll zone only — never the indicator zone.
        #    Rows 17-24: 5x8 glyphs at baseline 24 occupy exactly these rows;
        #    row 16 belongs to the journey scene's distance line (drawn once
        #    per reset — clearing it here would erase it until the next reset).
        self.draw_square(
            0,
            FLIGHT_NO_DISTANCE_FROM_TOP - FLIGHT_NO_TEXT_HEIGHT + 1,
            boundary,
            FLIGHT_NO_DISTANCE_FROM_TOP,
            colours.BLACK,
        )

        # 2. Indicator: draw once per state change, then leave untouched.
        #    On a live canvas any per-frame rewrite here is visible flicker.
        #    Runs before the text draw so the multi->single transition can't
        #    stamp black over text just drawn at full width.
        #    During an ISS pass the zone alternates with (or shows) the
        #    "ISS" badge so the pass stays visible from the flight display.
        show_badge = False
        if has_indicator:
            if iss_overhead:
                phase = (count // ISS_BADGE_PHASE_FRAMES) % 2
                show_badge = phase == 1
                indicator_state = (self._data_index, len(self._data), phase)
            else:
                indicator_state = (self._data_index, len(self._data))
        elif iss_overhead:
            show_badge = True
            indicator_state = "iss"
        else:
            indicator_state = None
        if self._indicator_state != indicator_state:
            self.draw_square(
                DATA_INDEX_POSITION[0],
                FLIGHT_NO_DISTANCE_FROM_TOP - FLIGHT_NO_TEXT_HEIGHT + 1,
                screen.WIDTH,
                FLIGHT_NO_DISTANCE_FROM_TOP,
                colours.BLACK,
            )
            if show_badge:
                graphics.DrawText(
                    self.canvas,
                    DATA_INDEX_FONT,
                    DATA_INDEX_POSITION[0],
                    DATA_INDEX_POSITION[1],
                    ISS_BADGE_COLOUR,
                    "ISS",
                )
            elif has_indicator:
                indicator_text = f"{self._data_index + 1}/{len(self._data)}"
                graphics.DrawText(
                    self.canvas,
                    DATA_INDEX_FONT,
                    DATA_INDEX_POSITION[0],
                    DATA_INDEX_POSITION[1],
                    DATA_INDEX_COLOUR,
                    indicator_text,
                )
            self._indicator_state = indicator_state

        # 3. Draw flight text, clipped per pixel column at the boundary.
        #    Characters enter column-by-column at x=52 exactly as they do
        #    at the hardware edge (x=64) in single-flight mode.
        flight_no_text_length = 0
        callsign = self._data[self._data_index]["callsign"]
        owner_icao = self._data[self._data_index]["owner_icao"]

        if callsign and callsign != "N/A":
            if owner_icao and callsign.startswith(owner_icao):
                flight_no = callsign[len(owner_icao):]
            else:
                flight_no = callsign

            iata_flight = self._data[self._data_index].get("flight_number", "")
            if iata_flight:
                flight_no = iata_flight

            airline = self._data[self._data_index].get("airline", "")
            if airline:
                main_text = f"{airline} {flight_no}"
            else:
                main_text = flight_no

            designator_len = 2 if iata_flight else 0
            alpha_len = (len(airline) + 1 if airline else 0) + designator_len

            for i, ch in enumerate(main_text):
                if i < alpha_len:
                    colour = FLIGHT_NUMBER_ALPHA_COLOUR
                else:
                    colour = (FLIGHT_NUMBER_NUMERIC_COLOUR
                              if ch.isnumeric()
                              else FLIGHT_NUMBER_ALPHA_COLOUR)

                char_x = self._scroll_pos + flight_no_text_length
                advance = FLIGHT_NO_CLIP_FONT.advance(ch)

                if char_x + advance <= boundary:
                    # Fully left of boundary — fast C++ draw
                    graphics.DrawText(
                        self.canvas,
                        FLIGHT_NO_FONT,
                        char_x,
                        FLIGHT_NO_DISTANCE_FROM_TOP,
                        colour,
                        ch,
                    )
                elif char_x < boundary:
                    # Straddles boundary — draw only columns < boundary
                    FLIGHT_NO_CLIP_FONT.draw_char_clipped(
                        self.canvas,
                        char_x,
                        FLIGHT_NO_DISTANCE_FROM_TOP,
                        colour,
                        ch,
                        x_max=boundary,
                    )
                # else: fully inside indicator zone — draw nothing

                flight_no_text_length += advance

        # Report width to shared scroll driver
        self.report_scroll_width("flight_details", flight_no_text_length)

    @Animator.KeyFrame.add(0)
    def reset_flight_details_scroll(self):
        # Runs via reset_scene() right before clear_screen() wipes the
        # canvas — the indicator must be redrawn on the next frame even
        # if the page state tuple is unchanged.
        self._indicator_state = "reset"
