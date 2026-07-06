"""Faithful headless stub of the rgbmatrix Python binding.

Pixel semantics (DrawText glyph placement, SwapOnVSync buffer handling,
SetPixel bounds behaviour) match hzeller/rpi-rgb-led-matrix lib/bdf-font.cc
and lib/led-matrix.cc as verified against the C++ source.
"""
import sys
import types


class _Recorder:
    """Global write log: frame -> list of (x, y, rgb) in draw order."""
    def __init__(self):
        self.frame = -1
        self.context = "?"  # name of the keyframe currently drawing
        self.writes = {}   # frame -> [(x, y, (r,g,b), context), ...]
        self.clears = []   # frames on which Clear() was called

    def log(self, x, y, rgb):
        self.writes.setdefault(self.frame, []).append((x, y, rgb, self.context))

    def zone_writes(self, frame, x0, x1, y0, y1):
        return [w for w in self.writes.get(frame, [])
                if x0 <= w[0] < x1 and y0 <= w[1] <= y1]


RECORDER = _Recorder()


class FrameCanvas:
    def __init__(self, width=64, height=32):
        self.width, self.height = width, height
        self.pixels = {}  # (x,y) -> (r,g,b); black pixels stored too

    def SetPixel(self, x, y, r, g, b):
        x, y = int(x), int(y)
        if 0 <= x < self.width and 0 <= y < self.height:
            self.pixels[(x, y)] = (int(r), int(g), int(b))
            RECORDER.log(x, y, (int(r), int(g), int(b)))

    def Clear(self):
        self.pixels = {}
        RECORDER.clears.append(RECORDER.frame)

    def snapshot(self, x0, x1, y0, y1, ignore_black=True):
        out = {}
        for (x, y), c in self.pixels.items():
            if x0 <= x < x1 and y0 <= y <= y1:
                if ignore_black and c == (0, 0, 0):
                    continue
                out[(x, y)] = c
        return out


class RGBMatrixOptions:
    pass


class RGBMatrix:
    def __init__(self, options=None):
        self.options = options
        self.brightness = getattr(options, "brightness", 100)
        self._active = FrameCanvas()  # initial internal framebuffer
        self.swap_count = 0

    def CreateFrameCanvas(self):
        return FrameCanvas()

    def SwapOnVSync(self, other, frame_fraction=1):
        # led-matrix.cc: passed canvas becomes the live buffer,
        # previous live buffer is returned.
        previous = self._active
        if other is not None:
            self._active = other
        self.swap_count += 1
        return previous


# ---- graphics submodule ----------------------------------------------------

class Color:
    def __init__(self, red=0, green=0, blue=0):
        self.red, self.green, self.blue = red, green, blue


class Font:
    def __init__(self):
        self._glyphs = {}  # codepoint -> (dwidth, height, y_offset, pixels)
        self.height = -1
        self.baseline = 0

    def LoadFont(self, path):
        # Same parse as bdf-font.cc (x_offset baked into columns,
        # columns clamped to < device_width).
        encoding = dwidth = bbx = rows = None
        with open(path, "r") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                key = parts[0]
                if key == "FONTBOUNDINGBOX":
                    self.height = int(parts[2])
                    self.baseline = self.height + int(parts[4])
                elif key == "ENCODING":
                    encoding = int(parts[1])
                elif key == "DWIDTH":
                    dwidth = int(parts[1])
                elif key == "BBX":
                    bbx = tuple(int(v) for v in parts[1:5])
                elif key == "BITMAP":
                    rows = []
                elif key == "ENDCHAR":
                    if encoding is not None and dwidth and bbx and rows is not None:
                        width, height, x_off, y_off = bbx
                        pixels = []
                        for ri, hex_row in enumerate(rows):
                            bits = int(hex_row, 16)
                            n_bits = 4 * len(hex_row)
                            for col in range(width):
                                if bits >> (n_bits - 1 - col) & 1:
                                    x = col + x_off
                                    if 0 <= x < dwidth:
                                        pixels.append((x, ri))
                        self._glyphs[encoding] = (dwidth, height, y_off, tuple(pixels))
                    encoding = dwidth = bbx = rows = None
                elif rows is not None and bbx and len(rows) < bbx[1]:
                    rows.append(parts[0])
        return True

    def _glyph(self, codepoint):
        return self._glyphs.get(codepoint) or self._glyphs.get(0xFFFD)

    def CharacterWidth(self, codepoint):
        g = self._glyphs.get(codepoint)
        return g[0] if g else -1

    def DrawGlyph(self, canvas, x, y, colour, codepoint):
        g = self._glyph(codepoint)
        if g is None:
            return 0
        dwidth, height, y_off, pixels = g
        top = y - height - y_off
        # bail-early like C++ (still returns advance)
        if x + dwidth < 0 or x > canvas.width or top + height < 0 or top > canvas.height:
            return dwidth
        for px, py in pixels:
            canvas.SetPixel(x + px, top + py, colour.red, colour.green, colour.blue)
        return dwidth


def DrawText(canvas, font, x, y, colour, text):
    total = 0
    for ch in text:
        total += font.DrawGlyph(canvas, x + total, y, colour, ord(ch))
    return total


def DrawLine(canvas, x0, y0, x1, y1, colour):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 <= x1 else -1
    sy = 1 if y0 <= y1 else -1
    if dx == 0:
        for y in range(y0, y1 + sy, sy):
            canvas.SetPixel(x0, y, colour.red, colour.green, colour.blue)
    elif dy == 0:
        for x in range(x0, x1 + sx, sx):
            canvas.SetPixel(x, y0, colour.red, colour.green, colour.blue)
    else:  # Bresenham
        err = dx - dy
        x, y = x0, y0
        while True:
            canvas.SetPixel(x, y, colour.red, colour.green, colour.blue)
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy


def install():
    """Register fake rgbmatrix + rgbmatrix.graphics in sys.modules."""
    rgb = types.ModuleType("rgbmatrix")
    gfx = types.ModuleType("rgbmatrix.graphics")
    gfx.Color = Color
    gfx.Font = Font
    gfx.DrawText = DrawText
    gfx.DrawLine = DrawLine
    rgb.graphics = gfx
    rgb.RGBMatrix = RGBMatrix
    rgb.RGBMatrixOptions = RGBMatrixOptions
    rgb.FrameCanvas = FrameCanvas
    sys.modules["rgbmatrix"] = rgb
    sys.modules["rgbmatrix.graphics"] = gfx
    return rgb
