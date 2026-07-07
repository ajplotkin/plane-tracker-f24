# ✈️ plane-tracker-rgb-pi (ajplotkin fork)

A Raspberry Pi + RGB LED matrix flight tracker: live overhead-flight display,
a pixel-faithful browser mirror, flight statistics, ISS pass tracking, and a
live ATC radio that auto-tunes to the traffic overhead.

**Lineage.** Original tracker by
[c0wsaysmoo](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi) (descended
from ColinWaddell's *its-a-plane-python*) → forked and re-architected by
[a10kiloham](https://github.com/a10kiloham/plane-tracker-rgb-pi) → forked
here. Work has flowed in BOTH directions: several of this author's features
were merged upstream into a10kiloham's repo (PRs #1/#2/#5/#6) and into
c0wsaysmoo's (#32/#33/#36/#40/#41), and five more are currently open at
c0wsaysmoo's (#43 ISS ephem, #44 ISS indicator, #46 pixel-clipped scrolling,
#47 animator pacing, #48 ATC radio). See [Credits](#credits) for the full
breakdown. This private repo is where the work happens; the public fork
exists for the PRs.

Requires a **paid FR24 subscription API key**. Runs on single or triple
Adafruit RGB Matrix Bonnets.

---

## What this fork adds

### Display correctness — the live-canvas discipline
The rgbmatrix canvas here is **live** (`sync()` discards `SwapOnVSync()`'s
return): there is no double buffering, so every rewritten pixel is visible
flicker. Every scene follows draw-on-change discipline, enforced by a
**headless test harness** (`testing/`) that replays the real display pipeline
against a faithful rgbmatrix stub and asserts pixel-level invariants
(indicator zone written only on page changes, row-ownership boundaries, logo
repaint after resets, idle scenes writing nothing). Display changes require
two consecutive clean harness passes before deploy. This eliminated the
page-indicator flicker, weather-icon flicker, scroll jitter (drift-corrected
animator pacing), and glyph clipping artifacts; the core fixes were PR'd
upstream (pixel-clipped scrolling via a dependency-free BDF clip renderer and
the animator pacing fix to c0wsaysmoo; pacing to a10kiloham). Both scroll
lines stay locked to one shared position — deliberate, and different from
both upstreams.

### Live ATC radio (LiveATC.net)
- **Auto-tune**: scores the airports of the flights actually overhead
  (destination-weighted, sticky, dwell + decay, a 250 mi relevance filter,
  distance tie-breaks); prefers approach feeds for traffic at 3–15k ft and
  tower below — and when everything overhead is a high-altitude overflight,
  tunes the nearest **ARTCC center sector** instead, because that is who
  those crews are talking to.
- **Self-healing**: candidates are probe-verified before tuning (only a real
  404 marks a feed dead, remembered 6 h; network errors never dead-mark),
  the current station re-verifies every 10 min, and probing has a
  circuit breaker (LiveATC rate-limits aggressive clients — respect them).
- **Verified seed**: 36 airports + NY-area center sectors, every mount name
  extracted from LiveATC's own pages (`docs/ATC Seed Update Method.md`);
  `ATC_CUSTOM_FEEDS` adds stations without touching the seed.
- **Outputs**: browser (the mirror plays the stream — zero Pi cost), Pi USB
  speaker (mpv), **Chromecast**, and **on-demand AirPlay** — both AirPlay 1
  and AirPlay 2 receivers (AirPort Express, HomePod, Apple TV, Macs), with a
  guided one-time pairing for any device that shows a code, and zero idle
  processes. Unified discovery at `/api/atc/outputs`; standard Cast/AirPlay
  pickers in the mirror; quiet hours with a play-again override; a
  Homebridge/HomeKit switch (`setup/homebridge/`) whose ON means "the
  tracker's own speaker" — and the same switch is exposed as a **Matter
  device** (Home Assistant + Matterbridge on the hub Pi), so Google Home and
  Apple Home can both turn the radio on/off natively
  (`docs/Raspberry Pi EH - Services.md`).

### Browser display mirror
Pixel-faithful canvas mirror of the LED panel at `/display`, synchronized via
a scroll-epoch contract (timestamps + cycle length in the API), with
self-reload on deploys, staleness gates, the ATC bar (play, station dropdown
with nearest airports/centers, output pickers), and an audio-resume overlay
for browser autoplay policy.

### Stats & web UI
Hour/day/airline/aircraft/route charts, route-frequency and great-circle
deviation analysis, mobile-friendly maps — and **click-to-drill**: every
chart segment expands to the actual underlying flight rows. Web config
editor (JSON overlay over `.env`) with live ATC controls; API usage
dashboard.

### ISS
Local ephemeris pass prediction (TLE via ephem — no external pass API),
an incremental takeover renderer (pixel-identical to a full repaint, without
the flicker), dwell rotation so every overhead flight still gets shown during
long passes, and an indicator-zone ISS badge.

### Local conditions
Beyond the temperature and 3-day forecast, the clock/date rotation surfaces a
set of local-conditions feeds (each optional, configured by station/coords):
- **Tides** — next high/low via the NOAA CO-OPS API.
- **Sea/water temperature** — NOAA CO-OPS water temp with an NDBC buoy
  fallback (`WATER_TEMP_STATION` / `WATER_TEMP_FALLBACK_STATION`).
- **Rain alerts** — minutely "rain starting in ~8 min" / "stopping" precip
  alerts via OpenWeatherMap One Call 3.0.
- **Airport status** — ground-stop / delay durations for nearby airports.
- **Nearest city / landmark** — reverse-geocoded place name for a flight's
  position (offline cities5000 database + landmark lookup).
- **NWS alerts** — severity-coloured weather-alert rotation (see
  `utilities/nws_alerts.py`).
- **UV index** — a chip beside the temperature, EPA/WHO-coloured. Interpolated
  from Tomorrow.io's hourly forecast curve to the current moment (refreshed
  every 15 min) rather than a single realtime snapshot, which is rate-limited
  and otherwise lags a fast-changing sky.

### Odds and ends
Heliport/helicopter detection, custom HELI/GA/ISS logos, moon-phase date
glyphs, sunrise/sunset clock colouring, minimum page-display time, per-glyph
clock updates, prebuilt aarch64 wheel for `miniaudio` in `setup/wheels/` (Pis
never compile).

## Provenance — who built what

**c0wsaysmoo (the foundation).** The tracker itself: scene system and display
design, journey/logo/weather scenes, the ISS takeover concept, the AirLabs /
route-lookup integrations, and his own local airport/airline handling (built
independently — see note below). Everything here stands on this.

**a10kiloham.** Official FR24 gRPC SDK integration (paid API), the caching
and rate-limiting architecture, systemd + `.env` + venv deployment with
secrets hygiene, `/var/lib` data directory, triple-bonnet support, startup
validation, one-command setup, and the first web pages (closest/farthest
maps).

**This fork (ajplotkin) — merged upstream into a1k's repo** (PRs
[#1](https://github.com/a10kiloham/plane-tracker-rgb-pi/pull/1),
[#2](https://github.com/a10kiloham/plane-tracker-rgb-pi/pull/2),
[#5](https://github.com/a10kiloham/plane-tracker-rgb-pi/pull/5),
[#6](https://github.com/a10kiloham/plane-tracker-rgb-pi/pull/6); his
integration commit `1f0d54f`): the local airport database (`airports.py`) and
airline database (`airlines.py`) with regional carrier overrides, the
enrichment pipeline in `overhead.py` (haversine from local coords instead of
trusting `flight_progress`, airline-name resolution, GA owner lookup via
adsbdb, None-coordinate guards), the per-call `fr24_client.py` rewrite (no
leaked connections 24/7), `SEARCH_RADIUS_NM` / `SPEED_UNITS` config, the
unit-test suite, the OOM/thread-accumulation fix, the farthest-flight
meters-vs-km fix, the persistent weather cache and rate limiter,
server-side callsign filtering and IATA→ICAO conversion for tracked flights.
(c0's repo has its own, independently written airport/airline modules —
parallel evolution, not a copy in either direction.)

**This fork — merged into c0's repo directly** (small fixes, all merged):
flight-number coloring for airlines with digits in the name
([#41](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/41)), the
OOM/thread-accumulation guard
([#40](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/40)),
CommuteAir→United Express override
([#36](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/36)), WiFi
status fix ([#33](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/33)),
and temperature regressions
([#32](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/32)).

**This fork — not (yet) upstream anywhere.** The live-canvas draw-on-change
discipline and the pixel-level headless test harness; pixel-clipped
scrolling with the dependency-free BDF clip renderer and drift-corrected
animator pacing (PR'd: c0
[#46](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/46)/[#47](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/47),
a1k [#8](https://github.com/a10kiloham/plane-tracker-rgb-pi/pull/8)); the ISS
ephem rewrite and incremental takeover renderer with dwell rotation + badge
(PR'd: c0
[#43](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/43)/[#44](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/44));
helicopter/heliport detection; the synchronized display mirror; the stats
pages with click-to-drill charts, deviation/frequency analysis and mobile
maps; the web config editor; the UV chip, NWS alerts, tides; and the entire
ATC radio (PR'd: c0
[#48](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi/pull/48)).

## Layout

- `its-a-plane-python/` — the app (deployed to the Pi)
- `testing/` — headless display harness (never deployed); `testing/TESTING.md`
- `docs/` — feature roadmap, ATC seed method, session reports
- `its-a-plane-python/setup/homebridge/` — HomeKit switch scripts
- `its-a-plane-python/setup/wheels/` — prebuilt wheels so Pis never compile

## Installation (delta over a10kiloham's process)

Base install follows a10kiloham's instructions (appendix below). This fork
adds the following — all optional unless noted:

**ATC audio outputs** (browser output needs nothing extra):
```bash
sudo apt install mpv ffmpeg          # USB-speaker player + AirPlay transcode
# NEVER let pip compile on a tracker Pi — wheels only:
sudo pip3 install --break-system-packages its-a-plane-python/setup/wheels/miniaudio-*.whl
sudo pip3 install --only-binary=:all: --break-system-packages pychromecast pyatv
# If pip refuses over apt-owned packages (typing_extensions, cryptography):
#   add --ignore-installed <that package> — and after a cryptography bump,
#   also reinstall pyopenssl the same way or `import OpenSSL` breaks.
```
The `setup/wheels/` miniaudio wheel is aarch64/cp313 (Debian trixie); other
ABIs must build their own wheel OFF-device (a Pi 4/5 or any matching-ABI
box) — an on-device build OOM-wedges 512 MB–1 GB Pis.

**ATC config keys** (config page or `.env`/config.json): `ATC_ENABLED`
(master switch, default off), `ATC_QUIET_HOURS` ("HH:MM-HH:MM", blank =
night window), `ATC_AUTO_RESUME`, `ATC_CUSTOM_FEEDS`
("ICAO/kind/mount[/lat/lon]" list — see `docs/ATC Seed Update Method.md`).
Mode/station/volume/output are runtime state via the web UI / API.

**Smart-home hooks (optional, nothing auto-installs):**
- `setup/homebridge/` — on/off/state scripts + accessory JSON that expose the
  **ATC radio as a HomeKit switch** via an EXISTING Homebridge on your network.
- **Matter route** (Google Home + Apple Home): a Home Assistant RESTful switch
  pointing at `/api/atc/start|stop|status`, bridged by Matterbridge's
  `matterbridge-hass` plugin — runs on any always-on box, not the tracker. Pair
  the bridge once in each ecosystem and the ATC switch shows up natively.
- The only hooks in *this* codebase are the ATC `/api/atc/*` endpoints above.
  The panel can additionally be driven as a Matter **light** (on/off + RGB) by
  a Home-Assistant MQTT-light definition talking to your own matrix bus — that
  lives in your HA config, not here.

**Deployment habits that this repo's history earned the hard way:** deploy
to one device and confirm before the rest; restart the service after any
template change (Jinja caches); never `pip` source-builds on a live Pi; and
be gentle with LiveATC — the built-in probe circuit breaker exists because
they ban IPs that hammer them.

---

## Credits

This project is a chain of forks; everything here stands on the work below.

- **[ColinWaddell](https://github.com/ColinWaddell/its-a-plane-python)** — the
  original *its-a-plane-python* that started it all.
- **[c0wsaysmoo](https://github.com/c0wsaysmoo/plane-tracker-rgb-pi)** — the
  RGB-matrix tracker this fork descends from: the scene system and display
  design, journey/logo/weather scenes, the ISS takeover concept, the route
  lookups, and the hardware + base-install guide preserved below. If this
  project is useful to you, the original author takes coffees at
  [paypal.me/c0wsaysmoo](https://paypal.me/c0wsaysmoo) and is on Reddit as
  [Mediocre-Opposite225](https://old.reddit.com/user/Mediocre-Opposite225/).
- **[a10kiloham](https://github.com/a10kiloham/plane-tracker-rgb-pi)** — the
  re-architecture this fork builds on: official FR24 gRPC SDK, the caching and
  rate-limiting layer, systemd + venv + `.env` deployment with secrets hygiene,
  the `/var/lib` data directory, triple-bonnet support, and the first web maps.
- **This fork (ajplotkin)** — see [What this fork adds](#what-this-fork-adds)
  and [Provenance](#provenance--who-built-what) above. Work has flowed in both
  directions: fixes merged into a1k (PRs #1/#2/#5/#6) and c0 (#32/#33/#36/#40/#41),
  with more open at c0 (#43/#44/#46/#47/#48) and a1k (#7/#8).

Everything from `## Hardware` down is c0wsaysmoo's original documentation,
lightly updated — the hardware list and Pi/bonnet bring-up are unchanged and
still the right starting point; for the application itself this fork follows
a10kiloham's systemd/venv deployment (`its-a-plane-python/setup/update-pi.sh`)
plus the [ATC/audio delta](#installation-delta-over-a10kilohams-process) above,
not c0's original `crontab`/`FlightRadarAPI` steps.

---

## What a10kiloham changed from c0's original

The re-architecture focused on reliability, API sustainability, and data
enrichment; the display scenes and visual design stayed largely intact. Rows
tagged **ajplotkin** originated in this fork and were merged into a1k's repo.

| Feature | c0wsaysmoo (original) | a10kiloham re-architecture | Origin |
|---------|----------------------|----------------------|--------|
| **FR24 API** | Unofficial `FlightRadarAPI` pip package (web scraping) | Official `fr24` gRPC SDK (v0.3.0+, paid subscription) | a10kiloham |
| **FR24 Feed Caching** | None — polls API every cycle | 90-second polling interval with TTL cache | a10kiloham |
| **FR24 Flight Detail Cache** | None — fetches details on every pass | 30-minute per-flight cache (checks cache first) | a10kiloham |
| **Weather API Rate Limiting** | None — can exhaust free tier quickly | 1 call/hour max, 1-hour backoff on HTTP 429 | a10kiloham |
| **Weather Cache** | None | 1-hour TTL for temperature & forecast data | a10kiloham |
| **Configuration** | Hardcoded values in `config.py` | Environment variables via `.env` file + systemd EnvironmentFile | a10kiloham |
| **Secrets Management** | API keys in source code | Secrets in `/etc/plane-tracker.env` (mode 0600, root-only) | a10kiloham |
| **Deployment** | `crontab @reboot` | systemd service with auto-restart, journald logging | a10kiloham |
| **Python Environment** | `pip install --break-system-packages` | Proper virtualenv (`.venv/`) with `requirements.txt` | a10kiloham |
| **Airport Coordinates** | Relied entirely on FR24 API (which doesn't provide them via gRPC) | Local offline database (`airports.json` from GitHub CSV) | ajplotkin |
| **Airline Names** | Only from FR24 `registered_owners` field | Local airline database with regional carrier overrides | ajplotkin |
| **Distance Calculations** | Only from `flight_progress` (meters from API) | Haversine from local airport coords, API fallback | ajplotkin |
| **Helicopter Detection** | None | Identifies helicopter types, shows HELI logo | ajplotkin |
| **GA Aircraft Lookup** | None | adsbdb.com lookup for N-number owner info | ajplotkin |
| **Search Radius** | Fixed bounding box only | Configurable `SEARCH_RADIUS_NM` | ajplotkin |
| **Unit Tests** | None | 67+ tests covering cache, utilities, databases | ajplotkin |
| **FR24 Client Architecture** | Persistent async context (can leak resources) | Per-call context manager (thread-safe, no leaks) | ajplotkin |
| **Data Directory** | Home folder (breaks with systemd ProtectHome) | `/var/lib/plane-tracker/` (writable, survives upgrades) | a10kiloham |
| **Pipeline Diagnostics** | None | Rich per-cycle summary logging (flights, sources, stats) | ajplotkin |
| **Startup Validation** | None | Checks API keys/config at boot, logs masked keys | a10kiloham |
| **Triple RGB Matrix** | Single bonnet only | Triple Matrix Bonnet support | a10kiloham |
| **Setup Script** | Manual multi-step process | One-command `update-pi.sh` (clone, venv, deps, service) | a10kiloham |

---

### a10kiloham's caching architecture

```
┌─────────────────────────────────────────────────────────┐
│                  utilities/cache.py                       │
├──────────────────┬──────────────────────────────────────┤
│  WeatherCache    │  FR24Cache                            │
│  ─────────────   │  ──────────                           │
│  • 1hr TTL       │  • Feed: 90s TTL + rate limiter       │
│  • 1hr rate cap  │  • Details: 30min per-flight cache    │
│  • 429 backoff   │  • Cache-first lookup pattern         │
│  • Shared limiter│  • Independent key/flight caching     │
│  (temp+forecast) │                                       │
└──────────────────┴──────────────────────────────────────┘
```

- **Weather (Tomorrow.io)**: After the first successful call, the API is polled at most once per hour. On HTTP 429, enters a 1-hour backoff before retrying. Cached data is served during both normal rate-limiting and backoff periods.

- **FR24 Live Feed**: Polled at most every 90 seconds. Repeated calls within 90s return the cached flight list. Different bounding box / airline queries are cached independently.

- **FR24 Flight Details**: Each flight's details are cached for 30 minutes by `flight_id`. The display checks cache before requesting any further flight data — if a hit exists, no API call is made.

---

## Real-Time Flight Tracking Added

You can now monitor specific flights directly on your clock. To get started, open a browser on any device connected to the same network and navigate to: http://[hostname].local:8080

Note: Use the hostname you chose during setup (e.g., if your Pi is flight@tracker, go to tracker.local:8080).

How to Track

- Search & Save: Enter a flight number to track it immediately. The system will verify if the flight is currently active; if it is not, you will have the option to save it so that tracking begins as soon as it departs. Please note that if an active flight returns a "Not Found" error, it is likely due to current tracking limitations.

- Callsign Format: enter either the ICAO callsign (e.g. `UAL1134`) or the
  IATA flight number (`UA1134`) — this fork converts IATA→ICAO for the lookup.
- Coverage: regional carriers are supported (the local airline database maps
  operators like Republic/SkyWest to their mainline brand); how much a given
  flight resolves still depends on what the FR24 feed returns for it.

Display Features

When tracking begins, the flight data will temporarily replace your three-day forecast with the following:

- Status Header: Displays the logo, airline name, and route. The text is color-coded to indicate if the flight is on time or delayed.
- Progress Visual: A dynamic progress bar with a moving arrow icon. If there isn't current live data during the flight such as crossing the ocean then the arrow icon will turn red. When there is no live data the tracker will calculate time and distance remaining until it refreshes with live data. 
- Flight Telemetry: The bottom line shows remaining time and distance, aircraft type, airspeed, and altitude (with an arrow indicating climbing or descending).
Once the flight reaches its destination, the display will automatically switch back to the weather forecast. In the meantime, the clock will continue to show overhead flights as usual.

This project is based on [Colin Waddell's work](https://github.com/ColinWaddell/its-a-plane-python), with some additional features I’ve added.

## Clock Screen:
- Displays time, date, current temperature, and a 3-day forecast.
- The current temperature color is based on the current humidity level on a gradient of white-blue.
- Time changes color at sunrise and sunset.
- The date shows moon phases with a purple-to-white gradient. It gradually becomes white on the right until the full moon, then fades white on the left as the moon wanes.
- The display dims at predefined times, set in the `.env` file (`NIGHT_START` / `NIGHT_END`).
- You can switch between 12hr/24hr time and choose imperial or metric units.

## Flight Tracker Screen:
- Displays the origin and destination airport codes, with distances to both airports.
- Airport codes are color-coded based on the difference between the scheduled and actual departure times, as well as the scheduled and estimated arrival times.

  **Departure:**
  - 0-20 mins: Green
  - 20-40 mins: Yellow
  - 40-60 mins: Orange
  - 1-4 hrs: Red
  - 4-8 hrs: Purple
  - 8+ hrs: Blue
  
  **Arrival:**
  - On-time or early: Green
  - 0-30 mins late: Yellow
  - 30-60 mins late: Orange
  - 1-4 hrs late: Red
  - 4-8 hrs late: Purple
  - 8+ hrs late: Blue
 
  - If either the actual arrival time is None (not updated yet) or actual departure time is None (not updated yet) the airport code will be Grey. Happens if you live close to an airport 

- An arrow between the airport codes acts as a progress bar for the flight, starting red (just left) and turning green (almost complete).
- Below, the airline’s IATA name, flight number, abbreviated aircraft type, and the distance/direction to your location are displayed.
- The airline's ICAO code is shown in the logo, indicating which airline is operating the flight. This is especially useful for regional carriers, where an airline might operate flights for multiple brands (e.g., Republic Airways flying for American Eagle, Delta Connection, and United Express).

Logs the closest flights to your location and farthest destinations

1. **Top N closest flights** to your location (`MAX_CLOSEST`)  
2. **Top N farthest flights** based on origin or destination (`MAX_FARTHEST`)  

Each time a flight is detected:  

- Calculates the **distance from home**  
- Updates `close.txt` and `farthest.txt` if a **new closest flight** or a **new top-N farthest flight** is found  
- Sends an **automatic email alert** when these changes occur with flight details and map 

**Email notifications:**  

- Includes a **link to an interactive map** showing flight positions (Link is good for 30 days. You can always view the maps on your local IP page)  

**Key details:**  

- Adjustable limits with `MAX_CLOSEST` and `MAX_FARTHEST`  
- Closest flights to your house are always updated in `close.txt`  
- Farthest destination/origin flights are maintained in `farthest.txt` independently  
- Alerts taper off as flight positions stabilize  
- Emails can be **turned off** while still keeping the log files and local wegpage. 

> **From c0wsaysmoo (original author):** please reread the instructions
> carefully if you hit issues — most problems come from a step being skipped.
> If you're stuck, reach out on Reddit
> ([Mediocre-Opposite225](https://old.reddit.com/user/Mediocre-Opposite225/)).
> If the project's useful to you, a coffee is always appreciated
> ([paypal.me/c0wsaysmoo](https://paypal.me/c0wsaysmoo)) — shared in good faith.
 
![tracker](https://github.com/user-attachments/assets/802a6c43-31d2-48dc-816b-4eb0ca0367e1)
![PXL_20241019_155956016](https://github.com/user-attachments/assets/91532d4f-3b6f-4a1b-9a26-43ffe5c6093d)
![PXL_20241019_165254031](https://github.com/user-attachments/assets/2e70bfcd-70ae-4acc-ba69-dde07c56a068)
![PXL_20241019_165305826](https://github.com/user-attachments/assets/5188780d-84ff-4111-8bde-9584d6a70df2)
![PXL_20241019_155500974](https://github.com/user-attachments/assets/5c3540e9-b699-41c8-8aef-32fb7a7f7b5d)
Had to remount the Pi since the display ribbon bumped into the panel 
![PXL_20241019_155518437](https://github.com/user-attachments/assets/2d6f4beb-59f1-4771-80ce-8bafd00cd1fc)
![PXL_20241019_155605121](https://github.com/user-attachments/assets/4b71b758-00c9-4586-a5a0-ad251696eb17)
![PXL_20241019_155629794](https://github.com/user-attachments/assets/f82088b8-e959-44e3-82f3-7207779cc659)
![PXL_20241019_155732297](https://github.com/user-attachments/assets/77a329c7-d9c2-4a33-ab07-b6f6a2bf6ded)
![signal-2025-12-01-080516_002](https://github.com/user-attachments/assets/887de831-c33f-4646-a97f-bf88dfb396d9)

The difference in size between P4 and P2.5 panel. I use P4 for the living room and P2.5 for my desk.

<img width="422" height="322" alt="distance" src="https://github.com/user-attachments/assets/354cda11-9f3d-4b04-ad8e-68ddfc3ec3e5" />

The close.txt file. Farthest.txt looks the same.
<img width="1878" height="1019" alt="flight" src="https://github.com/user-attachments/assets/4466a735-1b4d-4e28-b22b-4f171c5a58fd" />


Map will show the top 3 (by default) farthest flights, and the top 3 closest ping'd flights to your location. Solid lines is the flown section and dashed is unflown. Uses actual flight path travelled (if available) then uses calculated Great-circle distance for the remainder. If no flight path travelled available then uses Great-circle distance for both. (If you want to reset your maps to take advantage of the newer flight path data, delete the farthest.txt file and reboot)

![email](https://github.com/user-attachments/assets/491c5725-9c3d-413e-bee3-54d88ab9d696)

The email

![web](https://github.com/user-attachments/assets/a61177a2-b2ee-4720-bc50-3ba89a95bc61)

The local webpage to track flights or to look at your maps/logs

https://github.com/user-attachments/assets/0b4b1fd7-0fd1-4d9d-8753-bb8d455cce10

How the display looks while it is tracking a flight

---

## Hardware Overview:

This is what I used to make mine. Other than the Pi and the Bonnet you can use whatever you want. You will need a computer with a SD card reader to setup the Pi and to do the install. You won't need it after it is setup.
- [Raspberry Pi 3A+](https://www.adafruit.com/product/4027) You can use the Pi 3B+/Pi 4 as well. (If you use a Pi 4 you'll need to adjust the "GPIO_SLOWDOWN" in the config file since it's more powerful than the Pi 3). It's just more expensive and you don't need the ethernet jack. You can also get them at [Microcenter](https://www.microcenter.com/product/514076/raspberry-pi-3-model-a-board). I tried with a Pi Zero, but couldn't get rid of the flicking completely even with soldering. I have not tried with a Pi 5, it requires different instructions with the Bonnet. If someone gets it running on the Pi 5 please let me know and I'll update the instructions. 
- [Adafruit bonnet](https://www.adafruit.com/product/3211)
- [64x32 RGB P4 panel](https://www.adafruit.com/product/2278) (I used a P4 panel measuring approximately 10 inches by 5 inches. If you prefer a smaller screen, you can opt for P3 or P2.5 panels etc, as long as they are 64x32 in size. These are available on Amazon and other websites. If the colors appear inverted, adjust the display file by changing 'RGB' to 'RBG.')
- [Tinted acrylic](https://www.adafruit.com/product/4749) makes the screen so much easier to read and looks nicer 10/10 recommend. Keep in mind that the acrylic panel is slightly larger than the P4 screen when you make the case.
- [double sided tape](https://www.amazon.com/EZlifego-Multipurpose-Removable-Transparent-Household/dp/B07VNSXY31) (I use it to attach the acrylic to the panel)
- MicroSD card (any size)
- [5V 4A power supply](https://www.amazon.com/Facmogu-Switching-Transformer-Compatible-5-5x2-1mm/dp/B087LY41PV) (powers both the Pi and the bonnet)
- [CPU heatsink](https://www.adafruit.com/product/3084) (this is the smaller heatsink)
- [2x20 pin extender](https://www.microcenter.com/product/480891/schmartboard-inc-schmartboard-inc-short-2x20-female-stackable-headers-qty-4) to prevent the bonnet from resting on it (the smaller heatsink you may not need the extender, but a normal size heatsink you will)
- [Optional power button](https://www.microcenter.com/product/420422/mcm-electronics-push-button-switch-spst-red) (though not really necessary)
- Soldering iron only required for PWM bridge or power button. The Pi 3 seems to be ok without the PWM bridge, but anything less and you'll want to.
- The case I built using a strip of 2in x 1/4in wood that I clampted and glued togother.
- However my friend [made this case](https://makerworld.com/en/models/819892#profileId-762764) that you can 3D print. 
- M2.5 machine screws to screw the bonnet onto the Pi and to screw the Pi onto the case from Ace Hardware.

---

# Plane Tracker RGB Pi Setup Guide

> **Heads up (this fork):** steps 1–6 below (Pi OS, bonnet, RGB-matrix bindings)
> are current and apply as-is. Steps 7–10 are c0wsaysmoo's **original** app
> setup — the unofficial `FlightRadarAPI` package and `crontab @reboot`. This
> fork uses a10kiloham's deployment instead (official `fr24` SDK, virtualenv,
> systemd) via `its-a-plane-python/setup/update-pi.sh`; run that in place of
> steps 7–10, then apply the [ATC/audio delta](#installation-delta-over-a10kilohams-process).

Once you get your Raspberry Pi up and running, you can follow [this guide](https://linuxconfig.org/enabling-ssh-on-raspberry-pi-a-comprehensive-guide) to set up the project. 


### 1. Install Raspberry Pi OS Lite
Using the official Raspberry Pi Imager, go to `Other` and select **Raspberry Pi 64 OS Lite** (the Pi Zero only supports Raspberry Pi 32 OS lite). **Note** These instructions are for **Bookworm** AND **Trixie**
When using the Imager make sure these settings are selected to enable SSH and make sure your WIFI information is typed in EXACTLY or else it won't connect when turned on.


![edit](https://github.com/user-attachments/assets/3141a507-6746-4741-84ba-2c5a6f319004)
![wifi](https://github.com/user-attachments/assets/0669de7a-cb9c-4c2a-9129-8b044c088f9f)

Make sure you select the correct timezone since that is what is displayed on the clock. You can always change it later.
![ssh](https://github.com/user-attachments/assets/67d6fa8f-5ae3-4bf9-9f47-fbf78017ad78)

### 2. Connect via SSH
I use **[MobaXterm](https://mobaxterm.mobatek.net/)** on Windows to SSH into the Pi since it allows you to see the folder structure. Can just open the files from there and edit them instead of through the cmd prompt. After [SSH-ing into the Pi](https://www.fromdev.com/2025/04/how-to-ssh-into-raspberry-pi-a-step-by-step-guide.html), proceed with the following steps.

### 3. Install the Adafruit Bonnet
[Install the bonnet](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/) by following the instructions provided by Adafruit.

```
curl https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/main/rgb-matrix.sh > rgb-matrix.sh
sudo bash rgb-matrix.sh
```

You can solder a bridge between the 4 and 18 to enable PWM for less screen flicker and smoother scrolling. It is optional as it will work without the bridge.

# During the script:
 - Interface board type: Bonnet (Option 1)
 - Quality if soldered jumper, Convenience if not

**Test to make sure the panel works before you do anything else.** You're looking for "HELLO WORLD" yellow happy face, with HELLO in green and WORLD in red. If it's only partially displaying or displaying parts in the wrong color than reattach the bonnet to the Pi. Do not continue unless it runs the test script perfectly.

```
cd ~/rpi-rgb-led-matrix/examples-api-use/
```

If you DIDN'T solder 

```
sudo ./demo -D 1 runtext.ppm --led-rows=32 --led-cols=64 --led-limit-refresh=60 --led-slowdown-gpio=2 --led-gpio-mapping=adafruit-hat
```

If you DID solder

```
sudo ./demo -D 1 runtext.ppm --led-rows=32 --led-cols=64 --led-limit-refresh=60 --led-slowdown-gpio=2 --led-gpio-mapping=adafruit-hat-pwm
```

### 4. Install prerequisite software

```
cd ~
sudo apt-get update
sudo apt-get install -y \
    git \
    python3-pip \
    python3-dev \
    python3-setuptools \
    cython3 \
    build-essential \
    libgraphicsmagick++-dev
```

### 5. Build and install Python bindings for RGB Matrix

```
cd ~/rpi-rgb-led-matrix/bindings/python
make
sudo pip install . --break-system-packages
```

### 6. Install Git and Git the tracker

Clone the tracker:
```
cd ~
git clone https://github.com/c0wsaysmoo/plane-tracker-rgb-pi
```
If the bridge on the bonnet is soldered, you'll need to set `HAT_PWM_ENABLED=True` in your `.env` file. It's True by default

After cloning the files, move everything to the main folder, as some files need to be in /home/path/ rather than /home/path/plane-tracker-rgb-pi/ You'll need to combine the two logos folders since Github only allows 1,000 files per folder so I had to split them.
```
mv ~/plane-tracker-rgb-pi/* ~/
mkdir -p ~/logos
mv ~/logo/* ~/logos/
mv ~/logo2/* ~/logos/
rmdir ~/logo ~/logo2
```

# 7. Install Python dependencies

```
pip install pytz requests beautifulsoup4 FlightRadarAPI folium selenium pillow flask --break-system-packages
```
If **Bookworm**
```
sudo setcap 'cap_sys_nice=eip' /usr/bin/python3.11
```

If **Trixie**

```
sudo setcap 'cap_sys_nice=eip' /usr/bin/python3.13
```

# 8. Make the Script Executable

```
chmod +x ~/its-a-plane-python/its-a-plane.py
```

# 9. Edit the Environment Config

```
cp ~/plane-tracker-rgb-pi/.env.example ~/plane-tracker-rgb-pi/.env
nano ~/plane-tracker-rgb-pi/.env
```

Fill in your API keys (`FR24_API_KEY`, `TOMORROW_API_KEY`), location coordinates (`HOME_LAT`, `HOME_LON`, `ZONE_*`), and any other preferences. See `.env.example` for full documentation of all variables.

# 10. Run the Script

```
~/its-a-plane-python/its-a-plane.py
```
Set Up the Script to Run on Boot

To ensure the script runs on boot, use crontab -e to edit the cron jobs and add the following line:

```
@reboot sleep 60 && ~/its-a-plane-python/its-a-plane.py
```

You can also run it like so to create a log file in case there are issues. 
```
@reboot sleep 60 && ~/its-a-plane-python/its-a-plane.py >> ~/its-a-plane-python/workdammit.log 2>&1
```

Optional: Add a Power Button
If you'd like to add a power button, you can solder the button to the **GND/SCL** pins on the bonnet. Then, run the following commands:
```
git clone https://github.com/Howchoo/pi-power-button.git
./pi-power-button/script/install
```
