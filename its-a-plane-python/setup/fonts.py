import os
from rgbmatrix import graphics

# Fonts
DIR_PATH = os.path.dirname(os.path.realpath(__file__))
extrasmall = graphics.Font()
small = graphics.Font()
regular = graphics.Font()
regular_bold = graphics.Font()
regularplus = graphics.Font()
regularplus_bold = graphics.Font()
large = graphics.Font()
large_bold = graphics.Font()
extrasmall.LoadFont(f"{DIR_PATH}/../fonts/4x6.bdf")
small.LoadFont(f"{DIR_PATH}/../fonts/5x8.bdf")
regular.LoadFont(f"{DIR_PATH}/../fonts/6x13.bdf")
regular_bold.LoadFont(f"{DIR_PATH}/../fonts/6x13B.bdf")
regularplus.LoadFont(f"{DIR_PATH}/../fonts/7x13.bdf")
regularplus_bold.LoadFont(f"{DIR_PATH}/../fonts/7x13B.bdf")
large.LoadFont(f"{DIR_PATH}/../fonts/8x13.bdf")
large_bold.LoadFont(f"{DIR_PATH}/../fonts/8x13B.bdf")


# --- micro-kerning for the 5x8 face ------------------------------------------
#
# 5x8 is a fixed-width face: every glyph advances 5px, including the space. But
# the glyphs carry their own side bearings, so letters within a word end up
# 1-2px apart while a word space opens a 7px hole — three to four times the
# text's own rhythm, which reads as accidental gapping rather than word breaks.
# Two other glyphs sit at the extremes of the cell and need the opposite
# treatment: "+" inks all five columns with no right-side bearing, so "+44"
# renders as nine unbroken pixels and looks like one long bar.
#
# Scenes that draw character-by-character add this to each advance. Measured on
# the tracked stats line: word gaps 7px -> 5px, line 223px -> 209px, with the
# letter rhythm (2px) untouched.
# The face is in the NAME rather than a font argument on purpose. Guarding by
# `font is small` looked tidier but cannot be tested: tests/conftest.py stubs
# rgbmatrix and hands back the SAME MagicMock for every graphics.Font(), so
# every face is identical under test and the guard silently passes whatever it
# is given. A caller using 6x13 has to reach past this name to misuse it.
KERN_5X8 = {" ": -2, "+": 1}


def kern_5x8(ch):
    """Extra advance in pixels to apply AFTER drawing `ch` in the 5x8 face."""
    return KERN_5X8.get(ch, 0)
