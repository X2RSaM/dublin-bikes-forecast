"""Dublin Bikes prediction API.

Run from the project folder:
    export JCDECAUX_API_KEY=your_key_here
    uvicorn api.main:app --reload

Then open http://127.0.0.1:8000/docs to try it in your browser.

How it works:
  - Every 5 minutes, a background task fetches live station status from JCDecaux
    and keeps the last 2 hours in memory (needed for the "change in the last hour" features).
  - Each request builds features with src/features.py (the same code used in training)
    and runs both saved models.
"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import lightgbm as lgb
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from features import build_live_features  # noqa: E402

JCDECAUX_URL = "https://api.jcdecaux.com/vls/v1/stations"
POLL_SECONDS = 300
KEEP_HISTORY = pd.Timedelta(hours=2)

# ---- Load the trained models once, at startup -------------------------------
MODELS = ROOT / "models"
forecast_model = lgb.Booster(model_file=str(MODELS / "lightgbm_v1.txt"))
empty_model = lgb.Booster(model_file=str(MODELS / "empty_classifier_v1.txt"))
empty_meta = json.loads((MODELS / "empty_classifier_v1.json").read_text())
usual = pd.read_parquet(MODELS / "usual_behaviour.parquet")

FORECAST_FEATURES = ["station_id", "bikes_now", "capacity", "fill_ratio",
                     "change_last_15min", "change_last_30min", "change_last_60min",
                     "hour", "minute", "dayofweek", "typical_change"]
EMPTY_FEATURES = empty_meta["features"]
THRESHOLD = empty_meta["threshold"]

# ---- Live data kept in memory ------------------------------------------------
history = pd.DataFrame(columns=["time", "station_id", "bikes"])
stations = {}  # station_id -> name, address, lat, lon, capacity, open
last_fetch = None  # when we last called JCDecaux


def fetch_snapshot():
    """One call to JCDecaux. Returns readings and updates station details."""
    resp = requests.get(JCDECAUX_URL, timeout=20, params={
        "contract": "dublin", "apiKey": os.environ["JCDECAUX_API_KEY"]})
    resp.raise_for_status()
    rows = []
    for s in resp.json():
        sid = int(s["number"])
        stations[sid] = {
            "name": s.get("name"), "address": s.get("address"),
            "lat": s["position"]["lat"], "lon": s["position"]["lng"],
            "capacity": s.get("bike_stands"), "open": s.get("status") == "OPEN",
        }
        if s.get("status") == "OPEN" and s.get("last_update"):
            rows.append({"time": pd.Timestamp(s["last_update"], unit="ms", tz="UTC"),
                         "station_id": sid, "bikes": s.get("available_bikes")})
    return pd.DataFrame(rows)


def refresh():
    """Fetch a new snapshot and add it to the in-memory history."""
    global history, last_fetch
    try:
        snap = fetch_snapshot()
        history = pd.concat([history, snap], ignore_index=True)
        cutoff = pd.Timestamp.now(tz="UTC") - KEEP_HISTORY
        history = history[history["time"] >= cutoff].drop_duplicates()
        last_fetch = pd.Timestamp.now(tz="UTC")
        print(f"Fetched {len(snap)} stations; {len(history)} readings in memory")
    except Exception as exc:  # keep running even if one fetch fails
        print(f"Fetch failed: {type(exc).__name__}")


def refresh_if_stale():
    """On cloud platforms the background task may be paused between requests,
    so each request also fetches fresh data if the last fetch is too old."""
    if last_fetch is None or pd.Timestamp.now(tz="UTC") - last_fetch > pd.Timedelta(seconds=POLL_SECONDS):
        refresh()


async def poll_forever():
    while True:
        await asyncio.to_thread(refresh)
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(poll_forever())
    yield
    task.cancel()


app = FastAPI(title="Dublin Bikes Forecast API",
              description="Bikes in 30 minutes and the risk of a station running out.",
              lifespan=lifespan)


def predict_all():
    refresh_if_stale()
    if history.empty:
        raise HTTPException(503, "Collecting live data, try again in a minute.")

    records = history.astype({"station_id": int, "bikes": float})
    records["time"] = pd.to_datetime(records["time"], utc=True)
    capacity = pd.Series({sid: info["capacity"] for sid, info in stations.items()})
    feats = build_live_features(records, capacity, usual)
    if feats.empty:
        raise HTTPException(503, "No recent station readings available.")

    # LightGBM needs station_id as a category; it maps it back to the training stations
    feats["station_id"] = feats["station_id"].astype("category")
    change = forecast_model.predict(feats[FORECAST_FEATURES])
    p_empty = empty_model.predict(feats[EMPTY_FEATURES])

    out = []
    for i, row in feats.iterrows():
        sid = int(row["station_id"])
        info = stations.get(sid, {})
        bikes_30 = min(max(row["bikes_now"] + change[i], 0), row["capacity"])
        p = float(p_empty[i])
        # "high" = the alert threshold chosen on validation data; "medium" = half of it
        risk = "high" if p >= THRESHOLD else "medium" if p >= THRESHOLD / 2 else "low"
        out.append({
            "station_id": sid, "name": info.get("name"), "address": info.get("address"),
            "lat": info.get("lat"), "lon": info.get("lon"), "capacity": int(row["capacity"]),
            "bikes_now": int(row["bikes_now"]),
            "bikes_in_30_min": round(float(bikes_30), 1),
            "empty_risk": round(p, 3), "empty_risk_level": risk,
        })
    return out


@app.get("/")
def home():
    return {"message": "Dublin Bikes Forecast API. See /docs to try it."}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "readings_in_memory": len(history),
        "latest_reading": None if history.empty else str(history["time"].max()),
        "stations_known": len(stations),
    }


@app.get("/forecast")
def forecast_all():
    """Predictions for every station."""
    return predict_all()


@app.get("/forecast/{station_id}")
def forecast_station(station_id: int):
    """Prediction for one station, by its number."""
    for row in predict_all():
        if row["station_id"] == station_id:
            return row
    raise HTTPException(404, f"No live data for station {station_id}.")