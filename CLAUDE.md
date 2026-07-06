# plane-tracker-f24 — Agent Orientation

64x32 RGB LED flight tracker (its-a-plane-python lineage) + web mirror, for Raspberry Pi (Pi 3A+ class). `its-a-plane-python/` is what runs on the device.

## Layout
- `its-a-plane-python/` — the app
- `logo/` — airline/operator logo PNGs; `its-a-plane-python/logos` symlinks here
- `testing/` — headless display test harness (fake rgbmatrix + real pipeline). See `testing/TESTING.md`. Run both passes clean before shipping display changes.

## Critical architecture fact
`display/__init__.py sync()` discards `SwapOnVSync()`'s return → the canvas is the LIVE framebuffer; there is no double buffering. **Never rewrite a pixel whose content didn't change** — erase+redraw of identical content is visible flicker at the panel's 120Hz refresh. All scenes follow draw-on-change; keep it that way and extend `testing/e2e_debug.py` scenarios for new draw paths.

## Mirror contract
The JS mirror (`web/templates/display.html`) re-renders from JSON state (`.cache/*.json` via `/api/display-state`). Any rendering/behavior change on the Python side must be mirrored in the JS and, if it needs new state, added to the contract. Key fields: `scroll_epoch{ts,idx,max_width,pos,iss_plane_shown}`, `alerts.json` (blank slots = `text:null`), `processing`, `iss_live`, `utc_offset_sec`.

## ISS behavior
During a pass with flights: dwell rotation — 30s ISS slots alternating with one flight page per interlude (20s cameo cap protects against churny traffic); pages round-robin so every plane is seen. Indicator zone (x52–63) doubles as the pass badge: `N/M` ↔ `ISS` (steel blue) every 2s, steady `ISS` when single-flight.

## Configuration
All user-specific values (location, API keys, stations) come from environment variables — see `.env.example`. Never hardcode locations or keys in source.
