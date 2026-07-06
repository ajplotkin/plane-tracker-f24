"""Verification for utilities/textclip.py against the real BDF fonts."""
import os
import sys

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
sys.path.insert(0, WORK)

from utilities.textclip import ClipFont


class FakeCanvas:
    def __init__(self, w=64, h=32):
        self.w, self.h = w, h
        self.pixels = {}  # (x,y) -> (r,g,b)

    def SetPixel(self, x, y, r, g, b):
        self.pixels[(x, y)] = (r, g, b)


class FakeColour:
    red, green, blue = 255, 128, 0


FONT_5x8 = ClipFont(os.path.join(WORK, "fonts", "5x8.bdf"))
FONT_4x6 = ClipFont(os.path.join(WORK, "fonts", "4x6.bdf"))
BOUNDARY = 52
BASELINE = 24
failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def render_text(font, text, x0, y=BASELINE, x_max=10**9):
    c = FakeCanvas()
    x = x0
    for ch in text:
        font.draw_char_clipped(c, x, y, FakeColour, ch, x_max=x_max)
        x += font.advance(ch)
    return set(c.pixels)


def ascii_art(pixel_set, x_range, y_range, marker="#"):
    out = []
    for y in y_range:
        out.append("".join(marker if (x, y) in pixel_set else "." for x in x_range))
    return "\n".join(out)


# --- A. Visual sanity: glyph shapes must be recognizable ---------------------
print("=== 'UA1234' in 5x8 at baseline 24 (rows 16-24) ===")
px = render_text(FONT_5x8, "UA1234", 0)
print(ascii_art(px, range(0, 30), range(16, 25)))
print("=== '2/4' in 4x6 at (52,24) ===")
px_ind = render_text(FONT_4x6, "2/4", 52)
print(ascii_art(px_ind, range(52, 64), range(17, 25)))

# --- B. Advance widths equal DWIDTH (5 and 4) --------------------------------
import string
ok5 = all(FONT_5x8.advance(ch) == 5 for ch in string.printable if ch.isprintable())
ok4 = all(FONT_4x6.advance(ch) == 4 for ch in string.printable if ch.isprintable())
check("5x8 advance == 5 for all printable ASCII", ok5)
check("4x6 advance == 4 for all printable ASCII", ok4)

# --- C. Vertical placement: 5x8 glyphs at baseline 24 occupy rows 17-24 ------
px_all = render_text(FONT_5x8, string.ascii_letters + string.digits + "gjpqy/", 0)
rows = {y for (_, y) in px_all}
check("all rows within 17..24 (BBX 5 8 0 -1)", min(rows) >= 17 and max(rows) <= 24,
      f"rows={sorted(rows)}")

# --- D. Clip equivalence: clipped == unclipped filtered to x < boundary ------
all_ok = True
for start in range(30, 60):
    unclipped = render_text(FONT_5x8, "UA1234", start)
    clipped = render_text(FONT_5x8, "UA1234", start, x_max=BOUNDARY)
    expected = {(x, y) for (x, y) in unclipped if x < BOUNDARY}
    if clipped != expected:
        all_ok = False
        print(f"  mismatch at start={start}")
check("clipped render == unclipped ∩ {x < 52} for all offsets", all_ok)

# --- E. No pixel ever reaches the indicator zone -----------------------------
zone_clean = True
for start in range(-40, 70):
    clipped = render_text(FONT_5x8, "UNITED UA1234", start, x_max=BOUNDARY)
    if any(x >= BOUNDARY for (x, _) in clipped):
        zone_clean = False
check("no clipped pixel at x >= 52 across full scroll sweep", zone_clean)

# --- F. Column-by-column entry at the boundary (no pop-in) -------------------
# As a char scrolls left toward/into view at x=52, its visible column count
# grows by at most its per-column pixel count — never a full-glyph jump,
# and each new position reveals exactly one more source column.
prev_cols = set()
monotonic = True
reveal_one = True
for char_x in range(BOUNDARY, BOUNDARY - 8, -1):
    c = FakeCanvas()
    FONT_5x8.draw_char_clipped(c, char_x, BASELINE, FakeColour, "2", x_max=BOUNDARY)
    cols = {x - char_x for (x, _) in c.pixels}  # glyph-relative visible columns
    if not cols >= prev_cols:
        monotonic = False
    if len(cols - prev_cols) > 1:
        reveal_one = False
    prev_cols = cols
check("glyph columns revealed cumulatively while entering", monotonic)
check("at most one new column revealed per scroll step", reveal_one)

# --- G. Left-edge clipping unaffected (chars exit at x=0 cleanly) -------------
c = FakeCanvas()
FONT_5x8.draw_char_clipped(c, -3, BASELINE, FakeColour, "2", x_max=BOUNDARY)
check("no negative-x pixels when exiting left edge",
      all(x >= 0 for (x, _) in c.pixels))

# --- H. Replacement glyph fallback -------------------------------------------
adv = FONT_5x8.advance("☃")  # snowman, not in font
check("unknown char falls back (advance >= 0, no crash)", adv >= 0, f"adv={adv}")

print()
print(f"{'ALL PASS' if not failures else 'FAILURES: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
