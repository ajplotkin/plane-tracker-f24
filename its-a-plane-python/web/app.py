#!/usr/bin/python3
# Pin Flask to cores 0-1, leaving core 3 for display process
import os as _os
try:
    _os.sched_setaffinity(0, {0, 1})
except Exception:
    pass
from flask import Flask, render_template, jsonify, send_from_directory, request
import json
import os
import subprocess
import sys
import threading
import time as _time

# Ensure the parent directory is on sys.path so `config` and `utilities` resolve
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utilities.fr24_client import FR24Client

# Singleton FR24Client shared across all web requests (shares cache + rate limiter)
_fr24_client = FR24Client()

# /web is the folder that this file lives in
WEB_DIR = os.path.dirname(__file__)

app = Flask(
    __name__,
    template_folder=os.path.join(WEB_DIR, "templates"),
    static_folder=os.path.join(WEB_DIR, "static")
)

# Writable data directory (same as overhead.py uses)
DATA_DIR = os.environ.get("PLANE_TRACKER_DATA_DIR", "/var/lib/plane-tracker")
CLOSEST_FILE  = os.path.join(DATA_DIR, "close.txt")
FARTHEST_FILE = os.path.join(DATA_DIR, "farthest.txt")
TRACKED_FILE  = os.path.join(DATA_DIR, "tracked_flight.json")
MAPS_DIR      = os.path.join(DATA_DIR, "maps")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Could not load {path}: {e}")
        return default


def _build_cached_route(sched):
    """Build cached_route dict from AirLabs schedule data.
    Concept from c0wsaysmoo/plane-tracker-rgb-pi."""
    from utilities.overhead import _airport_coords
    origin = sched.get("origin", "")
    dest = sched.get("destination", "")
    o_coords = _airport_coords(origin)
    d_coords = _airport_coords(dest)
    # Compute arrival timestamp from dep + duration if available
    dep_ts = sched.get("dep_time_ts")
    duration = sched.get("duration")
    arr_ts = (dep_ts + duration * 60) if dep_ts and duration else None
    # Try to get airline name from local DB
    airline_name = ""
    try:
        from utilities.overhead import _airline_name_lookup
        airline_icao = sched.get("airline_icao", "")
        if airline_icao:
            airline_name = _airline_name_lookup(airline_icao) or ""
    except (ImportError, Exception):
        pass
    return {
        "origin": origin,
        "destination": dest,
        "origin_lat": o_coords.get("lat"),
        "origin_lon": o_coords.get("lon"),
        "dest_lat": d_coords.get("lat"),
        "dest_lon": d_coords.get("lon"),
        "airline_name": airline_name,
        "aircraft_type": "",
        "time_scheduled_departure": dep_ts,
        "time_scheduled_arrival": arr_ts,
        "cs_airline_iata": sched.get("cs_airline_iata", ""),
        "dep_time": sched.get("dep_time", ""),
        "arr_time": sched.get("arr_time", ""),
    }


def lookup_flight(callsign):
    """
    Try to find a live flight by callsign or flight number.
    Returns a dict with found=True/False and flight info if found.
    """
    callsign = callsign.strip().upper()
    original_callsign = callsign  # preserve for AirLabs (IATA works better)

    # Convert IATA (UA353) to ICAO (UAL353)
    from utilities.overhead import IATA_TO_ICAO
    if len(callsign) >= 3 and callsign[:2] in IATA_TO_ICAO and callsign[2:3].isdigit():
        icao_prefix = IATA_TO_ICAO.get(callsign[:2])
        if icao_prefix:
            callsign = icao_prefix + callsign[2:]

    try:
        api = _fr24_client

        # Server-side callsign filter (searches FR24's full worldwide feed)
        match = api.find_by_callsign(callsign)

        if not match:
            # Not airborne — try AirLabs for scheduled flight (use original IATA format)
            from utilities.airlabs import get_flight_schedule, get_flight_legs
            sched = get_flight_schedule(original_callsign)
            if sched:
                # Try operating carrier callsign from AirLabs
                op_callsign = (sched.get("flight_icao") or "").upper()
                if op_callsign and op_callsign != callsign:
                    match = api.find_by_callsign(op_callsign)

                # Try regional operator callsigns as fallback
                if not match:
                    from utilities.overhead import REGIONAL_OPERATORS
                    icao_prefix = callsign.rstrip("0123456789")
                    flight_num = callsign[len(icao_prefix):]
                    if icao_prefix in REGIONAL_OPERATORS:
                        for alt_prefix in REGIONAL_OPERATORS[icao_prefix]:
                            match = api.find_by_callsign(alt_prefix + flight_num)
                            if match:
                                break

                if match:
                    # Found via operating carrier — fall through to details below
                    pass
                else:
                    # Check for multiple legs (e.g., AA100 does JFK→LHR then LHR→JFK)
                    # Concept from c0wsaysmoo/plane-tracker-rgb-pi.
                    legs = get_flight_legs(original_callsign)
                    if len(legs) > 1:
                        results = []
                        for leg in legs:
                            cr = _build_cached_route(leg)
                            results.append({
                                "callsign": callsign,
                                "origin": leg.get("origin", ""),
                                "destination": leg.get("destination", ""),
                                "dep_time": leg.get("dep_time", ""),
                                "status": leg.get("status", ""),
                                "scheduled_departure": leg.get("dep_time_ts"),
                                "cached_route": cr,
                            })
                        return {
                            "found": True,
                            "multiple": True,
                            "callsign": callsign,
                            "flights": results,
                            "summary": f"{len(results)} legs found for {original_callsign} — select one",
                        }

                    # Single leg — schedule only, may not be trackable
                    trackable = not bool(REGIONAL_OPERATORS.get(
                        callsign.rstrip("0123456789"), []))
                    cr = _build_cached_route(sched)
                    result = {
                        "found": True,
                        "scheduled": True,
                        "trackable": trackable,
                        "callsign": callsign,
                        "number": sched.get("flight_number", callsign),
                        "airline": "",
                        "origin": sched.get("origin", "???"),
                        "destination": sched.get("destination", "???"),
                        "dep_time": sched.get("dep_time", ""),
                        "status": sched.get("status", ""),
                        "scheduled_departure": sched.get("dep_time_ts"),
                        "cached_route": cr,
                        "summary": f"Scheduled: {sched.get('flight_number', callsign)} {sched.get('origin', '?')}→{sched.get('destination', '?')} Dep {sched.get('dep_time', '?')}",
                    }
                    if not trackable:
                        result["warning"] = (
                            "This flight may use a regional operator callsign — "
                            "live tracking will be attempted but may not work"
                        )
                    return result
            if not match:
                return {"found": False}

        # Get full details for airline name and route
        details = api.get_flight_details(match)
        match.set_flight_details(details)

        airline = match.airline_name or ""
        origin = match.origin_airport_iata or "???"
        destination = match.destination_airport_iata or "???"
        number = match.number or callsign

        # Build cached route from live FR24 data
        from utilities.overhead import _airport_coords
        fp = details.get("flight_progress") or {} if details else {}
        time_info = details.get("time") or {} if details else {}
        sched = (time_info.get("scheduled") or {})
        real = (time_info.get("real") or {})
        est = (time_info.get("estimated") or {})
        o_coords = _airport_coords(origin)
        d_coords = _airport_coords(destination)
        cr = {
            "origin": origin, "destination": destination,
            "origin_lat": o_coords.get("lat"), "origin_lon": o_coords.get("lon"),
            "dest_lat": d_coords.get("lat"), "dest_lon": d_coords.get("lon"),
            "airline_name": airline, "aircraft_type": match.aircraft_code or "",
            "time_scheduled_departure": sched.get("departure"),
            "time_scheduled_arrival": sched.get("arrival"),
            "time_real_departure": real.get("departure"),
            "time_estimated_arrival": est.get("arrival"),
            "cs_airline_iata": "",
        }

        return {
            "found": True,
            "callsign": match.callsign,
            "number": number,
            "airline": airline,
            "origin": origin,
            "destination": destination,
            "scheduled_departure": sched.get("departure"),
            "cached_route": cr,
            "summary": f"{airline} {number} {origin}→{destination}",
        }

    except Exception as e:
        print(f"Lookup error: {e}")
        return {"found": False, "error": str(e)}


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(WEB_DIR, "static"), "favicon.ico", mimetype="image/x-icon")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/closest/json")
def closest_json():
    return jsonify(load_json(CLOSEST_FILE, []))


@app.get("/farthest/json")
def farthest_json():
    return jsonify(load_json(FARTHEST_FILE, []))


@app.get("/closest")
def closest_page():
    return render_template("closest_map.html")


@app.get("/farthest")
def farthest_page():
    return render_template("farthest_map.html")


@app.get("/tracked/json")
def tracked_json():
    return jsonify(load_json(TRACKED_FILE, {"callsign": ""}))


@app.post("/tracked/lookup")
def tracked_lookup():
    """Live lookup — check if a flight is currently findable before saving."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({"found": False, "error": "Invalid request"}), 400
    callsign = data.get("callsign", "").strip().upper()
    if not callsign:
        return jsonify({"found": False, "error": "No callsign provided"})
    result = lookup_flight(callsign)
    return jsonify(result)


@app.post("/tracked/set")
def tracked_set():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"message": "Invalid request"}), 400
    callsign = data.get("callsign", "").strip().upper()[:10]
    cached_route = data.get("cached_route")        # dict from lookup, or None
    sched_dep = data.get("scheduled_departure")     # unix timestamp, or None
    try:
        payload = {"callsign": callsign, "set_ts": int(_time.time()) if callsign else 0}
        if cached_route:
            payload["cached_route"] = cached_route
        if sched_dep:
            payload["scheduled_departure"] = sched_dep
        with open(TRACKED_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            os.chmod(TRACKED_FILE, 0o666)
        except OSError:
            pass
        # Clear stale mirror data when tracking is cleared
        if not callsign:
            try:
                from utilities.overhead import safe_write_json
                safe_write_json(os.path.join(DATA_DIR, "current_tracked.json"), {})
            except Exception:
                pass
        msg = f"Now tracking {callsign}." if callsign else "Tracking cleared."
        return jsonify({"message": msg})
    except Exception as e:
        return jsonify({"message": f"Error saving: {e}"}), 500


@app.post("/route/search")
def route_search():
    """Search for live flights by origin→destination using gRPC server-side filter."""
    import re
    data = request.get_json(force=True)
    if not data:
        return jsonify({"flights": [], "error": "Invalid request"}), 400
    origin = data.get("origin", "").strip().upper()
    destination = data.get("destination", "").strip().upper()
    if not origin or not destination:
        return jsonify({"flights": [], "error": "Origin and destination required"}), 400
    if not re.match(r'^[A-Z]{3,4}$', origin) or not re.match(r'^[A-Z]{3,4}$', destination):
        return jsonify({"flights": [], "error": "Airport codes must be 3-4 letters"}), 400
    try:
        matches = _fr24_client.find_by_route(origin, destination)
        flights = []
        for m in matches:
            flights.append({
                "callsign": m.callsign,
                "origin": m.origin_airport_iata or origin,
                "destination": m.destination_airport_iata or destination,
                "aircraft": m.aircraft_code or "",
                "altitude": m.altitude,
                "speed": m.ground_speed,
                "latitude": m.latitude,
                "longitude": m.longitude,
            })
        return jsonify({"flights": flights, "count": len(flights)})
    except Exception as e:
        return jsonify({"flights": [], "error": str(e)}), 500


# Location name (reverse geocode via Nominatim).
# Concept from c0wsaysmoo/plane-tracker-rgb-pi.
_location_cache = {}

@app.get("/airport-code")
def airport_code():
    """Return home airport code and reverse-geocoded location name."""
    if _location_cache:
        return jsonify(_location_cache)

    try:
        from config import JOURNEY_CODE_SELECTED, LOCATION_HOME
        code = JOURNEY_CODE_SELECTED or "???"
        lat, lon = LOCATION_HOME[0], LOCATION_HOME[1]
    except Exception:
        return jsonify({"code": "???", "name": "", "home_lat": None, "home_lon": None})

    import requests as http_req
    location_name = ""
    try:
        r = http_req.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 13},
            headers={"User-Agent": "plane-tracker-rgb-pi/1.0"},
            timeout=5,
        )
        if r.status_code == 200:
            addr = r.json().get("address", {})
            neighbourhood = (
                addr.get("neighbourhood")
                or addr.get("suburb")
                or addr.get("quarter")
                or addr.get("village")
            )
            city = addr.get("city") or addr.get("town") or addr.get("county")
            if neighbourhood and city:
                location_name = f"{neighbourhood}, {city}"
            elif city:
                location_name = city
    except Exception:
        pass

    result = {"code": code, "name": location_name, "home_lat": lat, "home_lon": lon}
    _location_cache.update(result)
    return jsonify(result)


@app.post("/api/airlines")
def api_airlines():
    """Batch-resolve ICAO airline prefixes to names.

    POST JSON: {"codes": ["UAL", "AAL", "DAL"]}
    Returns: {"UAL": "United Airlines", "AAL": "American Airlines", ...}
    """
    try:
        from utilities.airlines import get_airline_name
    except ImportError:
        return jsonify({})
    data = request.get_json(force=True) or {}
    codes = data.get("codes", [])
    result = {}
    for code in codes[:100]:  # cap at 100
        name = get_airline_name(code)
        if name:
            result[code] = name
    return jsonify(result)


@app.post("/api/airport-coords")
def api_airport_coords():
    """Batch-resolve airport codes to coordinates.

    POST JSON: {"codes": ["JFK", "LAX", "CDG"]}
    Returns: {"JFK": {"lat": 40.64, "lon": -73.78}, ...}
    """
    try:
        from utilities.airports import get_airport_coords
    except ImportError:
        return jsonify({})
    data = request.get_json(force=True) or {}
    codes = data.get("codes", [])
    result = {}
    for code in codes[:200]:  # cap at 200
        coords = get_airport_coords(code)
        if coords:
            result[code] = coords
    return jsonify(result)


@app.get("/api/aircraft-types")
def api_aircraft_types():
    """Return aircraft type code -> name mapping (from aircraft_types.json)."""
    path = os.path.join(BASE_DIR, "aircraft_types.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Build flat lookup: both primary code and short_codes -> name
        result = {}
        for code, info in data.items():
            name = info.get("name", code)
            result[code] = name
            for sc in info.get("short_codes", []):
                result[sc] = name
        return jsonify(result)
    except Exception:
        return jsonify({})


# Flight counter and stats (concept from c0wsaysmoo/plane-tracker-rgb-pi)
from utilities.overhead import COUNTER_FILE


@app.get("/counter")
def flight_counter():
    """Return full flight counter log (date-keyed dict)."""
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
        if not isinstance(log, dict):
            return jsonify({})
        return jsonify(log)
    except Exception:
        return jsonify({})


@app.get("/counter/summary")
def flight_counter_summary():
    """Return daily summary stats for graphing."""
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
        if not isinstance(log, dict):
            return jsonify([])
        summary = []
        for day, data in sorted(log.items()):
            by_hour = [0] * 24
            for flight in data.get("flights", []):
                h = int(flight.get("hour") or 0)
                if 0 <= h <= 23:
                    by_hour[h] += 1
            summary.append({
                "date": day,
                "count": data.get("count", 0),
                "by_hour": by_hour,
                "first_seen": data.get("first_seen", ""),
                "last_seen": data.get("last_seen", ""),
            })
        return jsonify(summary)
    except Exception:
        return jsonify([])


@app.get("/stats")
def stats_page():
    return render_template("stats.html")


@app.get("/stats/<date>")
def stats_day_page(date):
    return render_template("stats_day.html")


# Serve map files from the data directory
@app.get("/maps/<path:filename>")
def maps(filename):
    return send_from_directory(MAPS_DIR, filename)


# ---- Config UI ----

@app.get("/config")
def config_page():
    return render_template("config.html")


@app.get("/api/config")
def api_config_get():
    """Return current config as JSON. Masks secret values."""
    import config as cfg

    SECRET_KEYS = {"FR24_API_KEY", "TOMORROW_API_KEY", "AIRLABS_API_KEY", "NPS_API_KEY", "OWM_API_KEY"}

    result = {}
    # Flat env-style keys the UI expects
    for key in [
        "HOME_LAT", "HOME_LON",
        "ZONE_TL_LAT", "ZONE_TL_LON", "ZONE_BR_LAT", "ZONE_BR_LON",
        "JOURNEY_CODE_SELECTED", "TEMPERATURE_LOCATION", "TIDE_STATION",
        "WATER_TEMP_STATION", "AIRPORT_STATUS_LIST",
        "DISTANCE_UNITS", "SPEED_UNITS", "TEMPERATURE_UNITS", "CLOCK_FORMAT",
        "BRIGHTNESS", "BRIGHTNESS_NIGHT", "GPIO_SLOWDOWN", "LED_RGB_SEQUENCE",
        "NIGHT_BRIGHTNESS", "NIGHT_START", "NIGHT_END", "HAT_PWM_ENABLED",
        "MIN_ALTITUDE", "JOURNEY_BLANK_FILLER", "FORECAST_DAYS", "BLOCKED_CALLSIGNS",
        "NWS_ALERTS_ENABLED", "ISS_ALERTS_ENABLED",
        "HOURLY_CHIME_ENABLED", "HOURLY_CHIME_VOLUME",
        "HOURLY_CHIME_QUIET_START", "HOURLY_CHIME_QUIET_END",
        "MAX_CLOSEST", "MAX_FARTHEST", "STATS_LOG_DAYS",
        "FR24_API_KEY", "TOMORROW_API_KEY", "AIRLABS_API_KEY", "NPS_API_KEY", "OWM_API_KEY",
        "EMAIL",
        "ATC_ENABLED", "ATC_MODE", "ATC_STATION", "ATC_VOLUME", "ATC_OUTPUT",
        "ATC_AUTO_RESUME", "ATC_QUIET_HOURS", "ATC_CUSTOM_FEEDS",
    ]:
        # Return resolved booleans for checkbox fields
        if key in {"NIGHT_BRIGHTNESS", "HAT_PWM_ENABLED", "NWS_ALERTS_ENABLED", "ISS_ALERTS_ENABLED",
                   "HOURLY_CHIME_ENABLED", "ATC_ENABLED", "ATC_AUTO_RESUME"}:
            result[key] = getattr(cfg, key, False)
            continue
        val = cfg._get(key)
        if key in SECRET_KEYS and val:
            # Mask: show first 4 and last 4 chars
            if len(val) > 10:
                val = val[:4] + "*" * (len(val) - 8) + val[-4:]
            else:
                val = "****"
        result[key] = val

    # Populate computed zone/location fields if not already set
    for key, fallback in [
        ("HOME_LAT", str(cfg.LOCATION_HOME[0])),
        ("HOME_LON", str(cfg.LOCATION_HOME[1])),
        ("ZONE_TL_LAT", str(cfg.ZONE_HOME["tl_y"])),
        ("ZONE_TL_LON", str(cfg.ZONE_HOME["tl_x"])),
        ("ZONE_BR_LAT", str(cfg.ZONE_HOME["br_y"])),
        ("ZONE_BR_LON", str(cfg.ZONE_HOME["br_x"])),
    ]:
        if not result.get(key):
            result[key] = fallback

    return jsonify(result)


_VALID_CONFIG_KEYS = {
    "HOME_LAT", "HOME_LON",
    "ZONE_TL_LAT", "ZONE_TL_LON", "ZONE_BR_LAT", "ZONE_BR_LON",
    "JOURNEY_CODE_SELECTED", "TEMPERATURE_LOCATION", "TIDE_STATION",
    "WATER_TEMP_STATION", "AIRPORT_STATUS_LIST",
    "DISTANCE_UNITS", "SPEED_UNITS", "TEMPERATURE_UNITS", "CLOCK_FORMAT",
    "BRIGHTNESS", "BRIGHTNESS_NIGHT", "GPIO_SLOWDOWN", "LED_RGB_SEQUENCE",
    "NIGHT_BRIGHTNESS", "NIGHT_START", "NIGHT_END", "HAT_PWM_ENABLED",
    "MIN_ALTITUDE", "JOURNEY_BLANK_FILLER", "FORECAST_DAYS", "BLOCKED_CALLSIGNS",
    "NWS_ALERTS_ENABLED", "ISS_ALERTS_ENABLED",
    "MAX_CLOSEST", "MAX_FARTHEST", "STATS_LOG_DAYS",
    "FR24_API_KEY", "TOMORROW_API_KEY", "AIRLABS_API_KEY", "NPS_API_KEY", "OWM_API_KEY",
    "EMAIL",
    "ATC_ENABLED", "ATC_MODE", "ATC_STATION", "ATC_VOLUME", "ATC_OUTPUT",
    "ATC_AUTO_RESUME", "ATC_QUIET_HOURS", "ATC_CUSTOM_FEEDS",
    "HOURLY_CHIME_ENABLED", "HOURLY_CHIME_VOLUME",
    "HOURLY_CHIME_QUIET_START", "HOURLY_CHIME_QUIET_END",
}


@app.post("/api/config")
def api_config_post():
    """Save config to config/config.json and reload."""
    import config as cfg

    data = request.get_json(force=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    # Allowlist: only accept known config keys
    data = {k: v for k, v in data.items() if k in _VALID_CONFIG_KEYS}
    if not data:
        return jsonify({"error": "No valid config keys provided"}), 400

    # Ensure config directory exists
    config_dir = os.path.join(BASE_DIR, "config")
    os.makedirs(config_dir, exist_ok=True)

    config_path = os.path.join(config_dir, "config.json")

    # Load existing JSON config to merge (preserve keys not sent)
    existing = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    # Merge new values
    existing.update(data)

    # Atomic write: write to tmp then rename
    tmp_path = config_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp_path, config_path)
        try:
            os.chmod(config_path, 0o666)
        except OSError:
            pass
    except Exception as e:
        return jsonify({"error": f"Write failed: {e}"}), 500

    # Reload config module
    try:
        cfg.reload()
    except Exception as e:
        return jsonify({"error": f"Saved but reload failed: {e}"}), 500

    return jsonify({"status": "ok", "source": cfg.config_source()})


@app.get("/api/system")
def api_system():
    """System status: uptime, CPU temp."""
    info = {"uptime": "", "cpu_temp": "", "config_source": "env"}

    try:
        import config as cfg
        info["config_source"] = cfg.config_source()
    except Exception:
        pass

    # Uptime (Linux)
    try:
        with open("/proc/uptime", "r") as f:
            secs = float(f.read().split()[0])
            days = int(secs // 86400)
            hours = int((secs % 86400) // 3600)
            mins = int((secs % 3600) // 60)
            if days > 0:
                info["uptime"] = f"{days}d {hours}h {mins}m"
            elif hours > 0:
                info["uptime"] = f"{hours}h {mins}m"
            else:
                info["uptime"] = f"{mins}m"
    except Exception:
        info["uptime"] = "N/A"

    # CPU temp (Raspberry Pi)
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_mc = int(f.read().strip())
            info["cpu_temp"] = f"{temp_mc / 1000:.1f}°C"
    except Exception:
        info["cpu_temp"] = "N/A"

    # Load average
    try:
        load1, load5, load15 = os.getloadavg()
        info["load_avg"] = f"{load1:.2f} / {load5:.2f} / {load15:.2f}"
    except Exception:
        info["load_avg"] = "N/A"

    # Service uptime
    try:
        service = os.environ.get("SERVICE_NAME", "flight-tracker")
        result = subprocess.run(
            ["systemctl", "show", service, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5
        )
        ts_line = result.stdout.strip()  # ActiveEnterTimestamp=Sat 2026-06-14 00:15:32 EDT
        if "=" in ts_line:
            ts_str = ts_line.split("=", 1)[1].strip()
            if ts_str:
                from datetime import datetime as _dt
                # Parse systemctl timestamp
                start = subprocess.run(
                    ["date", "-d", ts_str, "+%s"],
                    capture_output=True, text=True, timeout=5
                )
                start_epoch = float(start.stdout.strip())
                svc_secs = _time.time() - start_epoch
                days = int(svc_secs // 86400)
                hours = int((svc_secs % 86400) // 3600)
                mins = int((svc_secs % 3600) // 60)
                if days > 0:
                    info["service_uptime"] = f"{days}d {hours}h {mins}m"
                elif hours > 0:
                    info["service_uptime"] = f"{hours}h {mins}m"
                else:
                    info["service_uptime"] = f"{mins}m"
    except Exception:
        info["service_uptime"] = "N/A"

    return jsonify(info)


# ---- ATC Audio (O1) ----

def _atc():
    """Lazily get the ATC audio manager singleton (never fails hard)."""
    from utilities.atc_audio import get_manager
    return get_manager()


@app.get("/api/atc/status")
def atc_status():
    try:
        return jsonify(_atc().status())
    except Exception as e:
        return jsonify({"error": str(e), "enabled": False, "mode": "off"}), 200


@app.get("/api/atc/outputs")
def atc_outputs():
    """Unified output discovery: [{id, name, type}] (review note 6).
    ?rescan=1 forces a fresh mDNS/AirPlay scan."""
    try:
        force = request.args.get("rescan") in ("1", "true", "yes")
        return jsonify({"outputs": _atc().list_outputs(force_rescan=force)})
    except Exception as e:
        return jsonify({"outputs": [
            {"id": "browser", "name": "This browser", "type": "browser"},
            {"id": "usb", "name": "Pi USB speaker", "type": "usb"},
        ], "error": str(e)})


@app.get("/api/atc/stations")
def atc_stations():
    """Full station list; ?nearby=1 returns the distance-ordered airport/feed
    groups for the selector dropdown (O2) — passive, never probes."""
    try:
        if request.args.get("nearby") in ("1", "true", "yes"):
            return jsonify({"nearby": _atc().nearby_stations()})
        return jsonify({"stations": _atc().stations()})
    except Exception as e:
        return jsonify({"stations": [], "nearby": [], "error": str(e)})


@app.post("/api/atc/start")
def atc_start():
    try:
        return jsonify(_atc().start())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/atc/stop")
def atc_stop():
    try:
        return jsonify(_atc().stop())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/atc/mode")
def atc_mode():
    data = request.get_json(force=True) or {}
    mode = (data.get("mode") or "").strip().lower()
    try:
        return jsonify(_atc().set_mode(mode))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/atc/station")
def atc_station():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(_atc().set_station(data.get("station", "")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/atc/volume")
def atc_volume():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(_atc().set_volume(data.get("volume", 70)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Cap concurrent relay streams: each holds a Flask worker thread plus an
# ffmpeg fetching LiveATC for its whole life — unbounded clients could
# exhaust the thread pool, and a burst of upstream fetches from one IP is
# exactly the pattern LiveATC's edges ban.
_RELAY_SEMAPHORE = threading.Semaphore(4)


@app.get("/atc/relay")
def atc_relay():
    """LOOPBACK-ONLY stream fetch adapter. pyatv (AirPlay) cannot set a
    User-Agent and LiveATC's edges 403 library UAs — so the Pi's own players
    fetch through here, which adds a browser UA. This is NOT a rebroadcast:
    Loopback (pyatv/AirPlay) and private-LAN clients (Chromecast pulls the
    relay instead of hitting LiveATC with its own UA from the house IP) are
    allowed; anything global is refused — the relay serves this household's
    own receivers, it is not an internet rebroadcast (AJ call, 2026-07-02).
    ?fmt=raw skips the WAV transcode and proxies the source MP3 untouched
    (cast devices decode live MP3 natively; only pyatv needs WAV)."""
    import ipaddress as _ipa
    try:
        _ip = _ipa.ip_address(request.remote_addr)
        _ok = _ip.is_loopback or _ip.is_private
    except ValueError:
        _ok = False
    if not _ok:
        return jsonify({"error": "local clients only"}), 403
    import re as _re
    # fullmatch beats the old .replace("_","").isalnum(): isalnum() accepts
    # arbitrary Unicode letters/digits, which landed verbatim in an ffmpeg
    # URL. Mounts are plain ASCII [a-z0-9_]; cap length as a backstop.
    code = (request.args.get("code") or "").strip()[:64]
    if not code or not _re.fullmatch(r"[a-z0-9_]+", code, _re.IGNORECASE):
        return jsonify({"error": "bad code"}), 400
    fmt = (request.args.get("fmt") or "").strip()
    from flask import Response as _Resp
    ua = ("Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    import shutil as _sh
    # One semaphore slot per live response, held for its whole life: the
    # generators release it in their finally (Werkzeug close()s the response
    # iterable even on client abort, so the finally always runs).
    if not _RELAY_SEMAPHORE.acquire(timeout=2):
        return jsonify({"error": "relay busy"}), 503
    try:
        # -rw_timeout (µs) on both ffmpeg fetches: a stalled upstream edge
        # otherwise blocks the worker thread + ffmpeg pair forever.
        if fmt == "mp3" and _sh.which("ffmpeg"):
            # Cast path: re-encode the 16 kbps trickle to 128 kbps MP3. Identical
            # audio, 8x the bytes — the Chromecast receiver's startup buffer fills
            # in seconds instead of the better part of a minute.
            import subprocess as _sp
            proc = _sp.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-rw_timeout", "15000000",
                 "-user_agent", ua, "-i", f"https://d.liveatc.net/{code}",
                 "-vn", "-acodec", "libmp3lame", "-b:a", "128k",
                 "-ar", "44100", "-ac", "2",
                 "-map_metadata", "-1", "-bitexact",
                 "-f", "mp3", "-"],
                stdout=_sp.PIPE, stderr=_sp.DEVNULL)
            def gen():
                try:
                    while True:
                        chunk = proc.stdout.read(8192)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    _RELAY_SEMAPHORE.release()
            return _Resp(gen(), content_type="audio/mpeg")
        if fmt != "raw" and _sh.which("ffmpeg"):
            # Transcode to WAV: pyatv/miniaudio cannot INIT its decoder on a
            # trickling live MP3 (16 kbps = seconds per frame-sniff buffer), but
            # WAV inits from a 44-byte header. Verified: local MP3 file streams
            # fine, live MP3 URL fails init, WAV flows. Loopback-only, so the
            # ~1.4 Mbps PCM never leaves the Pi; CPU cost of decoding 16 kbps
            # mono is negligible.
            # -map_metadata -1 -bitexact is REQUIRED: without it ffmpeg inserts a
            # LIST/INFO chunk between fmt and data; dr_wav skips chunks via
            # seek-from-CURRENT, which pyatv's live source can't do, so the parser
            # degenerates into an endless 4-byte scan of the stream — a healthy
            # AirPlay session playing eternal silence (root-caused 2026-07-02).
            import subprocess as _sp
            proc = _sp.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-rw_timeout", "15000000",
                 "-user_agent", ua, "-i", f"https://d.liveatc.net/{code}",
                 "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                 "-map_metadata", "-1", "-bitexact",
                 "-f", "wav", "-"],
                stdout=_sp.PIPE, stderr=_sp.DEVNULL)
            def gen():
                # On a pipe ffmpeg writes 0xFFFFFFFF for the RIFF and data chunk
                # sizes (can't seek back to patch them). dr_wav then reads to EOF
                # during INIT to learn the frame count — which never comes on a
                # live stream. Rewrite both fields to a real, huge size (2 GB ≈
                # 3.4 h of PCM); the AirPlay reconnect loop covers the rollover.
                import struct as _st
                data_size = 0x7FFF0000
                try:
                    head = proc.stdout.read(44)
                    if not (len(head) == 44 and head[:4] == b"RIFF"
                            and head[36:40] == b"data"):
                        # Not the canonical 44-byte header (LIST chunk snuck
                        # in, or ffmpeg died and the read came back short or
                        # empty). Streaming it UN-patched hands pyatv the
                        # eternal-silence parser bug this rewrite exists to
                        # prevent — abort loudly instead; the client gets a
                        # short body and its reconnect loop retries.
                        print(f"ATC relay: unexpected WAV header from ffmpeg "
                              f"for '{code}' (len={len(head)}) — aborting "
                              f"stream", flush=True)
                        return
                    head = (head[:4] + _st.pack("<I", 36 + data_size)
                            + head[8:40] + _st.pack("<I", data_size))
                    yield head
                    while True:
                        chunk = proc.stdout.read(8192)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    _RELAY_SEMAPHORE.release()
            return _Resp(gen(), content_type="audio/wav")
        import requests as _rq
        up = _rq.get(f"https://d.liveatc.net/{code}", stream=True, timeout=10,
                     headers={"User-Agent": ua})
        def gen():
            try:
                for chunk in up.iter_content(8192):
                    yield chunk
            finally:
                up.close()
                _RELAY_SEMAPHORE.release()
        return _Resp(gen(), status=up.status_code,
                     content_type=up.headers.get("Content-Type", "audio/mpeg"))
    except BaseException:
        # Failed before a generator took ownership of the slot (e.g. the
        # upstream GET raised) — release here or the slot leaks.
        _RELAY_SEMAPHORE.release()
        raise


@app.post("/api/atc/airplay/pair")
def atc_airplay_pair():
    """One-time pairing for AirPlay devices that ask for a code.
    {output: id} begins (device shows PIN) -> {pin: "1234"} finishes.
    {cancel: true} aborts."""
    data = request.get_json(force=True) or {}
    try:
        if data.get("cancel"):
            return jsonify(_atc().airplay_pair_cancel())
        if data.get("status"):
            return jsonify(_atc().airplay_pair_status())
        if data.get("pin"):
            return jsonify(_atc().airplay_pair_finish(data["pin"]))
        out = (data.get("output") or "").strip()
        if not out:
            return jsonify({"ok": False, "error": "output required"}), 400
        return jsonify(_atc().airplay_pair_begin(out))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/atc/select-output")
def atc_select_output():
    data = request.get_json(force=True) or {}
    out = (data.get("output") or "").strip()
    if not out:
        return jsonify({"error": "output required"}), 400
    try:
        return jsonify(_atc().select_output(out))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- API Usage ----

from utilities.api_usage import get_usage as _api_get_usage

@app.get("/api/usage")
def api_usage():
    """Return API usage data as JSON."""
    return jsonify(_api_get_usage())


@app.get("/usage")
def usage_page():
    return render_template("usage.html")


@app.post("/api/restart")
def api_restart():
    """Restart the flight-tracker service via systemctl."""
    service = os.environ.get("SERVICE_NAME", "flight-tracker")
    try:
        subprocess.Popen(["sudo", "systemctl", "restart", service])
        return jsonify({"status": "restarting", "service": service})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- WiFi Management (concept from c0wsaysmoo/plane-tracker-rgb-pi) ----

@app.get("/api/wifi/status")
def wifi_status():
    """Return current WiFi connection info via nmcli."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY",
             "device", "wifi", "list"],
            capture_output=True, text=True, timeout=10
        )
        networks = []
        connected_ssid = None
        seen = {}  # ssid -> index in networks list
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            active = parts[0]
            ssid = parts[1]
            signal = parts[2]
            security = ":".join(parts[3:])
            if not ssid:
                continue
            if active == "yes":
                connected_ssid = ssid
            # If already seen, upgrade to active if this BSSID is the connected one
            if ssid in seen:
                if active == "yes":
                    networks[seen[ssid]]["active"] = True
                    networks[seen[ssid]]["signal"] = int(signal) if signal.isdigit() else 0
                continue
            entry = {
                "ssid": ssid,
                "signal": int(signal) if signal.isdigit() else 0,
                "security": security.strip(),
                "active": active == "yes",
            }
            seen[ssid] = len(networks)
            networks.append(entry)
        networks.sort(key=lambda x: x["signal"], reverse=True)

        ip_result = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", "wlan0"],
            capture_output=True, text=True, timeout=5
        )
        ip_addr = ""
        for line in ip_result.stdout.splitlines():
            if "IP4.ADDRESS" in line:
                ip_addr = line.split(":")[-1].split("/")[0].strip()
                break

        return jsonify({
            "connected_ssid": connected_ssid,
            "ip_address": ip_addr,
            "networks": networks,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/wifi/scan")
def wifi_scan():
    """Trigger a fresh nmcli scan and return updated network list."""
    try:
        subprocess.run(
            ["sudo", "nmcli", "device", "wifi", "rescan"],
            capture_output=True, text=True, timeout=15
        )
        _time.sleep(2)
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY",
             "device", "wifi", "list"],
            capture_output=True, text=True, timeout=10
        )
        networks = []
        seen = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            active = parts[0]
            ssid = parts[1]
            signal = parts[2]
            security = ":".join(parts[3:])
            if not ssid:
                continue
            if ssid in seen:
                if active == "yes":
                    networks[seen[ssid]]["active"] = True
                    networks[seen[ssid]]["signal"] = int(signal) if signal.isdigit() else 0
                continue
            seen[ssid] = len(networks)
            networks.append({
                "ssid": ssid,
                "signal": int(signal) if signal.isdigit() else 0,
                "security": security.strip(),
                "active": active == "yes",
            })
        networks.sort(key=lambda x: x["signal"], reverse=True)
        return jsonify({"networks": networks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/wifi/connect")
def wifi_connect():
    """Connect to a WiFi network. Body: { ssid, password }"""
    try:
        data = request.get_json(force=True) or {}
        ssid = (data.get("ssid") or "").strip()
        password = (data.get("password") or "").strip()
        if not ssid:
            return jsonify({"success": False, "error": "SSID is required"}), 400
        cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return jsonify({"success": True, "message": f"Connected to {ssid}"})
        else:
            err = (result.stderr or result.stdout).strip()
            return jsonify({"success": False, "error": err})
    except subprocess.TimeoutExpired:
        # Timeout usually means Pi switched networks — connection dropped, not failed
        return jsonify({
            "success": True,
            "switched": True,
            "message": "Connection in progress — the Pi may have switched networks. "
                       "Reconnect your device and navigate to the Pi's new IP.",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── Display Mirror ─────────────────────────────────────────────────────────

@app.get("/display")
def display():
    resp = app.make_response(render_template("display.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/api/display-state")
def api_display_state():
    """Single endpoint returning everything the display mirror needs.

    All data comes from in-memory caches — zero API calls, zero extra
    processing unless someone is actively viewing the display page.
    """
    import time as _t
    from datetime import datetime, timezone

    CACHE_DIR = os.path.join(BASE_DIR, ".cache")

    # --- Current overhead flights (written each grab cycle by overhead.py) ---
    overhead_raw = load_json(os.path.join(DATA_DIR, "current_overhead.json"), {})
    # Handle both old format (plain list) and new format ({flights, ts})
    if isinstance(overhead_raw, list):
        flights = overhead_raw
        overhead_ts = 0
    else:
        flights = overhead_raw.get("flights", [])
        overhead_ts = overhead_raw.get("ts", 0)

    # --- Tracked flight (live data, written each grab cycle) ---
    tracked = load_json(os.path.join(DATA_DIR, "current_tracked.json"), {})

    # --- Temperature + humidity ---
    temp_data = load_json(os.path.join(CACHE_DIR, "temperature.json"), {})
    temp_vals = temp_data.get("data", [None, None, None])
    temperature = temp_vals[0] if len(temp_vals) > 0 else None
    humidity = temp_vals[1] if len(temp_vals) > 1 else None
    # UV: interpolate the hourly forecast curve to now (matches the LED chip and
    # never lags the 30-min realtime snapshot); the display process writes the
    # shared hourly_uv.json this reads. Fall back to the realtime cached value.
    try:
        from utilities.temperature import get_current_uv
        uv_index = get_current_uv()
    except Exception:
        uv_index = temp_vals[2] if len(temp_vals) > 2 else None

    # --- Forecast ---
    forecast_data = load_json(os.path.join(CACHE_DIR, "forecast.json"), {})
    forecast = forecast_data.get("data", [])

    # --- Sunrise / sunset ---
    sun_data = load_json(os.path.join(CACHE_DIR, "suntimes.json"), {})
    sun = sun_data.get("data", {})

    # --- Tides ---
    tide_data = load_json(os.path.join(CACHE_DIR, "tides.json"), {})

    # --- Date display (synced from date scene) ---
    date_display = load_json(os.path.join(CACHE_DIR, "date_display.json"), {})

    # --- Scroll epochs (written by display loop / overhead.py) ---
    scroll_epoch = load_json(os.path.join(CACHE_DIR, "scroll_epoch.json"), {})
    tracked_route_epoch = load_json(os.path.join(CACHE_DIR, "tracked_route_epoch.json"), {})
    tracked_stats_epoch = load_json(os.path.join(CACHE_DIR, "tracked_stats_epoch.json"), {})

    # --- ISS ---
    iss_data = load_json(os.path.join(CACHE_DIR, "iss.json"), {})
    iss_passes = iss_data.get("passes", [])

    # --- Alerts (pre-formatted by clock scene, written to alerts.json) ---
    # The display process builds the exact same alert list it shows on the LED
    # and writes it to .cache/alerts.json with slot index for sync. Zero duplication.
    alerts_cache = load_json(os.path.join(CACHE_DIR, "alerts.json"), {})

    # --- Loading pulse + live ISS visibility (mirror contract) ---
    processing = load_json(os.path.join(CACHE_DIR, "processing.json"), {})
    iss_live = load_json(os.path.join(CACHE_DIR, "iss_live.json"), {})

    # --- Config ---
    try:
        import config as cfg
        clock_format = getattr(cfg, "CLOCK_FORMAT", "12hr")
        distance_units = getattr(cfg, "DISTANCE_UNITS", "imperial")
        temperature_units = getattr(cfg, "TEMPERATURE_UNITS", "imperial")
        speed_units = getattr(cfg, "SPEED_UNITS", "knots")
    except Exception:
        clock_format = "12hr"
        distance_units = "imperial"
        temperature_units = "imperial"
        speed_units = "knots"

    from datetime import datetime as _dt
    try:  # deploy marker: open mirror tabs self-reload when this changes
        ui_version = str(os.path.getmtime(
            os.path.join(BASE_DIR, "web", "templates", "display.html")))
    except OSError:
        ui_version = "0"

    # --- ATC audio (review note 4: one poll — folded in for the mirror) ---
    try:
        atc = _atc().display_state()
    except Exception:
        # enabled:False is load-bearing: the mirror JS tests
        # `atc.enabled === false` to hide the bar — omitting it rendered
        # an empty ATC bar whenever the manager threw.
        atc = {"enabled": False, "mode": "off", "station": "", "stream_url": "",
               "playing": False, "in_quiet_hours": False}

    return jsonify({
        "server_time": __import__('time').time(),  # for clock skew correction
        "ui_version": ui_version,
        # Pi's UTC offset so a remote browser renders the panel's local time
        "utc_offset_sec": _dt.now().astimezone().utcoffset().total_seconds(),
        "processing": processing,      # {processing, ts} — loading pulse
        "iss_live": iss_live,          # {visible, ts} — live theme flips
        "flights": flights,
        "flights_ts": overhead_ts,
        "temperature": temperature,
        "humidity": humidity,
        "uv_index": uv_index,
        "forecast": forecast,
        "sunrise": sun.get("sunrise"),
        "sunset": sun.get("sunset"),
        "tides": tide_data,
        "date_display": date_display,
        "scroll_epoch": scroll_epoch,
        "tracked_route_epoch": tracked_route_epoch,
        "tracked_stats_epoch": tracked_stats_epoch,
        "iss_passes": iss_passes,
        "tracked": tracked,
        "alerts_cache": alerts_cache,  # {alerts, slot, cycle_secs, ts}
        "clock_format": clock_format,
        "distance_units": distance_units,
        "temperature_units": temperature_units,
        "speed_units": speed_units,
        "atc": atc,                    # {mode, station, stream_url, playing, output, volume}
    })


@app.get("/logos/<filename>")
def serve_logo(filename):
    logos_dir = os.path.join(BASE_DIR, "logos")
    resp = send_from_directory(logos_dir, filename)
    resp.headers["Cache-Control"] = "public, max-age=86400"  # 24h browser cache
    return resp


@app.get("/icons/<filename>")
def serve_icon(filename):
    icons_dir = os.path.join(BASE_DIR, "icons")
    resp = send_from_directory(icons_dir, filename)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ---- ATC auto-tune background thread ----
# Advances the auto-tuner off the LED display hot loop. Reads
# current_overhead.json (written by the display process); spawns/tears down
# Pi-side audio backends only when a non-browser output is actively playing.
_atc_ticker_started = False


def _start_atc_ticker():
    global _atc_ticker_started
    if _atc_ticker_started:
        return
    _atc_ticker_started = True
    import threading as _t

    def _loop():
        while True:
            try:
                _atc().tick()
            except Exception:
                pass
            _time.sleep(5)

    try:
        t = _t.Thread(target=_loop, name="atc-ticker", daemon=True)
        t.start()
    except Exception:
        pass


def _kill_orphan_relay_transcoders():
    """Relay ffmpeg processes are children of THIS app; a service restart
    orphans them and they keep fetching LiveATC forever (observed: two
    orphans after restarts — exactly the traffic pattern that gets the IP
    banned). Sweep them at startup."""
    try:
        out = subprocess.run(["pgrep", "-f", "d.liveatc" + ".net"],
                             capture_output=True, text=True, timeout=10)
        for pid in out.stdout.split():
            try:
                with open(f"/proc/{pid}/comm") as f:
                    if f.read().strip() == "ffmpeg":
                        subprocess.run(["kill", pid], timeout=5)
            except Exception:
                pass
    except Exception:
        pass


# Started from __main__ only: the web app is always launched as
# `python web/app.py` (a subprocess of its-a-plane.py), so a bare
# `import app` (tests, config allowlist introspection) never spawns the
# ticker thread. (The old WERKZEUG_RUN_MAIN guard was always true.)
if __name__ == "__main__":
    _kill_orphan_relay_transcoders()
    _start_atc_ticker()
    app.run(host="0.0.0.0", port=8080, debug=False)
