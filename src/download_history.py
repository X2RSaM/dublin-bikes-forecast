"""Download 12 months of Dublin Bikes history and combine it into one file.

Source: Dublin City Council via data.gov.ie (CC BY 4.0).

Run from the project folder:
    python src/download_history.py

What it does:
  1. Downloads each monthly CSV into data/historical/ (skips months you already have).
  2. Checks each file has the columns we expect, and skips any that don't.
  3. Combines all months into one compact Parquet file for fast loading:
       data/historical/station_status.parquet   (one row per station reading)
       data/historical/stations.csv             (station names and locations)
"""

from pathlib import Path

import pandas as pd
import requests

BASE = ("https://data.smartdublin.ie/dataset/33ec9fe2-4957-4e9a-ab55-c5e917c7a9ab"
        "/resource/{rid}/download/dublin-bikes_station_status_{mmyyyy}.csv")

# month -> resource ID on the open data portal
MONTHS = {
    "2025-07": "5b5afbcc-61e0-48d0-870e-a22df2ea4793",
    "2025-08": "1cb1e957-3600-4cfc-abf7-6d5749108d4e",
    "2025-09": "921175fc-4182-4616-b981-35da50dacea1",
    "2025-10": "c9556189-9df4-4391-a0c4-18c3b8bc9f54",
    "2025-11": "e432e75f-ad0d-44ff-8e09-2a5ab88972e5",
    "2025-12": "89b9cc0c-e1da-4993-ba99-b64c70953d4e",
    "2026-01": "afff7586-654b-40e3-8c65-e76ce88a1ccc",
    "2026-02": "91098ee8-7ea7-4c70-b96d-face618a02b6",
    "2026-03": "33bbe7a6-3b30-49cf-a13d-0ef622dbedb1",
    "2026-04": "92857912-1a36-4c8e-bd85-d26177609dbf",
    "2026-05": "03dd67ef-33b0-4102-8329-42c67fdbf53e",
    "2026-06": "fdce169c-3164-46b3-bc71-fb8684e50ef4",
}

HIST_DIR = Path(__file__).resolve().parents[1] / "data" / "historical"

# Columns we keep for modelling (names, addresses etc. go in a separate small file)
STATUS_COLS = ["last_reported", "station_id", "num_bikes_available",
               "num_docks_available", "is_installed", "is_renting", "capacity"]
STATION_COLS = ["station_id", "name", "address", "lat", "lon", "capacity"]


def download(month: str, rid: str) -> Path:
    """Download one month's CSV, unless it's already on disk."""
    yyyy, mm = month.split("-")
    path = HIST_DIR / f"{month}.csv"
    if path.exists() and path.stat().st_size > 1000:
        print(f"{month}: already downloaded, skipping")
        return path

    url = BASE.format(rid=rid, mmyyyy=f"{mm}{yyyy}")
    print(f"{month}: downloading...", end=" ", flush=True)
    tmp = path.with_suffix(".part")
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    tmp.rename(path)  # only rename once the download fully finished
    print(f"done ({path.stat().st_size / 1e6:.0f} MB)")
    return path


def load_month(month: str, path: Path):
    """Read one month and check it looks right. Returns None if it doesn't."""
    df = pd.read_csv(path)
    missing = set(STATUS_COLS + STATION_COLS) - set(df.columns)
    if df.empty:
        print(f"{month}: file is empty, skipping")
        return None
    if missing:
        print(f"{month}: missing columns {sorted(missing)}, skipping")
        return None
    return df


def main():
    HIST_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Download
    paths = {}
    for month, rid in MONTHS.items():
        try:
            paths[month] = download(month, rid)
        except requests.RequestException as exc:
            print(f"\n{month}: download failed ({exc}), skipping")

    # 2. Load and check each month
    print()
    status_parts, latest_month_df = [], None
    for month, path in paths.items():
        df = load_month(month, path)
        if df is None:
            continue
        print(f"{month}: {len(df):,} rows, {df['station_id'].nunique()} stations")
        status_parts.append(df[STATUS_COLS])
        latest_month_df = df

    if not status_parts:
        print("No usable months found.")
        return

    # 3. Combine, clean, and save
    status = pd.concat(status_parts, ignore_index=True)
    status["last_reported"] = pd.to_datetime(status["last_reported"], utc=True)
    before = len(status)
    status = status.drop_duplicates(["station_id", "last_reported"])
    status = status.sort_values(["station_id", "last_reported"]).reset_index(drop=True)

    out = HIST_DIR / "station_status.parquet"
    status.to_parquet(out, index=False)

    stations = (latest_month_df[STATION_COLS]
                .drop_duplicates("station_id", keep="last")
                .sort_values("station_id"))
    stations.to_csv(HIST_DIR / "stations.csv", index=False)

    print()
    print(f"Combined {len(status_parts)} months: {len(status):,} rows "
          f"({before - len(status):,} duplicates removed)")
    print(f"From {status['last_reported'].min()} to {status['last_reported'].max()}")
    print(f"Saved {out.name} ({out.stat().st_size / 1e6:.0f} MB) and stations.csv "
          f"({len(stations)} stations)")


if __name__ == "__main__":
    main()