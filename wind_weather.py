"""Optional, cached current 10 m wind for a selected observing site.

Open-Meteo supplies model-based current conditions.  This is deliberately
separate from the astronomical ephemerides for the selected observing date.
Data licence: CC BY 4.0, with an attribution link in the plot UI.
"""
import json
import math
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SOURCE_URL = "https://open-meteo.com/en/docs"
API_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 10 * 60
_cache = {}
_lock = threading.Lock()


def current_wind(latitude, longitude):
    """Return validated wind at the given coordinates in m/s, degrees FROM north."""
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Invalid observatory coordinates")
    key = (round(latitude, 4), round(longitude, 4))
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return dict(cached[1])

    params = urlencode({
        "latitude": latitude, "longitude": longitude,
        "current": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms", "timezone": "UTC",
    })
    request = Request(f"{API_URL}?{params}", headers={"User-Agent": "SkyPlanner/0.7"})
    with urlopen(request, timeout=5) as response:
        data = json.load(response)
    current = data.get("current") or {}
    speed = float(current["wind_speed_10m"])
    direction = float(current["wind_direction_10m"])
    if not (math.isfinite(speed) and speed >= 0 and math.isfinite(direction) and 0 <= direction <= 360):
        raise ValueError("Invalid wind data from weather provider")
    units = data.get("current_units") or {}
    if units.get("wind_speed_10m") != "m/s":
        raise ValueError("Unexpected wind speed unit from weather provider")
    result = {
        "speed_ms": speed,
        "from_degrees": direction % 360,
        "time_utc": str(current.get("time", "")),
        "source": "Open-Meteo",
        "source_url": SOURCE_URL,
    }
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return dict(result)
