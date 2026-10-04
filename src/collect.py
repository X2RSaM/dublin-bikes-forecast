"""Collect one snapshot of Dublin Bikes station status plus current Dublin weather.

Each run appends one row per station to data/raw/YYYY-MM-DD.csv (UTC date).
Designed to be run every ~15 minutes by GitHub Actions, but works locally too:

    export JCDECAUX_API_KEY=your_key_here
    python src/collect.py
"""

import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

JCDECAUX_URL = "https://api.jcdecaux.com/vls/v1/stations"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
DUBLIN_LAT, DUBLIN_LON = 53.3498, -6.2603

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"

FIELDS = [
    "collected_at",
    "station_id",
    "name",
    "address",
    "lat",
    "lon",
    "status",
    "banking",
    "bike_stands",
    "available_bikes",
    "available_bike_stands",
    "last_update",
    "temperature_c",
    "precipitation_mm",
    "wind_speed_kmh",
    "weather_code",
]


def get_json(url: str, params: dict, retries: int = 3) -> object:
    """GET a JSON endpoint with simple retries and backoff."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            # Don't print the full exception: the URL can contain the API key.
            status = getattr(getattr(exc, "response", None), "status_code", "n/a")
            print(f"Request to {url} failed (attempt {attempt}/{retries}, status {status})")
            if attempt == retries:
                raise
            time.sleep(5 * attempt)


def fetch_stations(api_key: str) -> list:
    return get_json(JCDECAUX_URL, {"contract": "dublin", "apiKey": api_key})


def fetch_weather() -> dict:
    """Current Dublin weather. Returns empty values if the weather API fails,
    so a weather outage never stops bike data being collected."""
    try:
        data = get_json(
            WEATHER_URL,
            {
                "latitude": DUBLIN_LAT,
                "longitude": DUBLIN_LON,
                "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
                "timezone": "UTC",
            },
        )
        cur = data.get("current", {})
        return {
            "temperature_c": cur.get("temperature_2m"),
            "precipitation_mm": cur.get("precipitation"),
            "wind_speed_kmh": cur.get("wind_speed_10m"),
            "weather_code": cur.get("weather_code"),
        }
    except requests.RequestException:
        print("Weather fetch failed; continuing without weather for this snapshot.")
        return {"temperature_c": None, "precipitation_mm": None,
                "wind_speed_kmh": None, "weather_code": None}


def ms_to_iso(ms) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def build_rows(stations: list, weather: dict, collected_at: datetime) -> list:
    rows = []
    for s in stations:
        pos = s.get("position") or {}
        rows.append({
            "collected_at": collected_at.isoformat(),
            "station_id": s.get("number"),
            "name": s.get("name"),
            "address": s.get("address"),
            "lat": pos.get("lat"),
            "lon": pos.get("lng"),
            "status": s.get("status"),
            "banking": s.get("banking"),
            "bike_stands": s.get("bike_stands"),
            "available_bikes": s.get("available_bikes"),
            "available_bike_stands": s.get("available_bike_stands"),
            "last_update": ms_to_iso(s.get("last_update")),
            **weather,
        })
    return rows


def append_rows(rows: list, collected_at: datetime) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{collected_at:%Y-%m-%d}.csv"
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> int:
    api_key = os.environ.get("JCDECAUX_API_KEY")
    if not api_key:
        print("JCDECAUX_API_KEY is not set.")
        return 1

    collected_at = datetime.now(timezone.utc).replace(microsecond=0)
    stations = fetch_stations(api_key)
    if not stations:
        print("API returned no stations; nothing written.")
        return 1

    weather = fetch_weather()
    rows = build_rows(stations, weather, collected_at)
    path = append_rows(rows, collected_at)
    print(f"Wrote {len(rows)} station rows to {path.name} at {collected_at.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())