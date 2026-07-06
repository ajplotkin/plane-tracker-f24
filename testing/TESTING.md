# Headless Display Testing Harness

Tests the REAL display pipeline (`its-a-plane-python/display` + all scenes, real animator, real BDF fonts) on any machine — no LED matrix, no Pi, no network. Lives here in `testing/`, **not** inside `its-a-plane-python/`, so it never ships to the device.

## Files
- `fake_rgbmatrix.py` — faithful stub of the rgbmatrix binding. Pixel semantics ported from the C++ sources (`bdf-font.cc` glyph placement, `led-matrix.cc` SwapOnVSync buffer handling, SetPixel bounds). Records every pixel write tagged with frame number and the keyframe (scene method) that made it.
- `e2e_debug.py` — stubs the network modules (`overhead`, `temperature`, `iss`, `rain`, `nws_alerts`, `airport_status`, `tides`), imports the repo's real `display` module, and replays `Animator.play()` deterministically with scripted data injection.
- `test_textclip.py` — unit tests for `utilities/textclip.py` (the BDF clip renderer) against the repo's actual font files.

## Running
```bash
source ~/venv/bin/activate
cd testing
python test_textclip.py                                  # unit tests
python e2e_debug.py ../its-a-plane-python <scenario>     # end-to-end
```
To run against a scratch copy instead of the repo tree, build it with
`cp -RL its-a-plane-python/ <workdir>/` — the `-L` matters: `logos/` is a
symlink to `../logo` (gitignored), and a plain `cp -R` copies it dangling,
which makes the `reset` scenario's logo-repaint assertion fail misleadingly
(the harness now detects this and exits with a SETUP ERROR).

Exit code 0 = all assertions passed. Scenarios:

| Scenario | Proves |
|---|---|
| `multi` | Indicator zone written only on page changes; content pixel-exact; row-ownership invariants (flight 17–24, plane ≥25, journey ≤16) |
| `single` | Full-width scroll, no indicator |
| `reset` | Indicator repaints after a data-change reset |
| `iss` | Flight scene silent during takeover; repaints on resume |
| `issfull` | ISS renderer writes incrementally (no per-frame Clear); dumps per-frame canvas snapshots (optional 3rd arg = JSON path) for pixel-diffing two code versions |
| `isscameo` | Plane cameo → takeover holds for the whole pass |
| `isscap` | Takeover within 20s even under continuous new-flight churn |
| `idle` | Clock-page scenes rewrite nothing when content is unchanged (flicker regression) |

## Core invariant
`sync()` passes the canvas to `SwapOnVSync()` and discards the return, so the canvas IS the live framebuffer — there is no double buffering. Any pixel rewritten with the same content, or cleared and redrawn within a frame, is visible flicker at the panel's 120Hz refresh. The harness enforces this as: **a region's pixels may be written only when its content changes.** When adding a scene or changing a draw path, add/extend a scenario here and require two consecutive clean passes.

## Pixel-diffing two versions
`issfull` writes end-of-frame snapshots to JSON. Run it against two checkouts and diff (see `docs/Flight Tracker — Full Review v3 (JS, Flicker, ISS).md` for the pattern used to prove the incremental ISS renderer pixel-identical to the original).
