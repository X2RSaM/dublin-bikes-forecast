"""Turn raw station readings into a modelling table.

Used by the notebooks now, and later by the prediction API, so that training
and live predictions always build features in exactly the same way.
"""

import pandas as pd

SLOT_MINUTES = 15


def load_grid(path, max_fill=2):
    """Read station_status.parquet and return:
    grid      - one row per 15-minute slot (UTC), one column per station, values = bikes
    capacity  - docks per station
    Short gaps (up to `max_fill` slots) are filled with the last known value.
    """
    status = pd.read_parquet(path)
    status = status[status["is_installed"] & status["is_renting"] & (status["capacity"] > 0)]
    status = status.assign(slot=status["last_reported"].dt.floor(f"{SLOT_MINUTES}min"))

    grid = status.pivot_table(index="slot", columns="station_id",
                              values="num_bikes_available", aggfunc="last")
    full = pd.date_range(grid.index.min(), grid.index.max(), freq=f"{SLOT_MINUTES}min")
    grid = grid.reindex(full).ffill(limit=max_fill)
    grid.index.name = "slot"

    capacity = status.groupby("station_id")["capacity"].last()
    return grid, capacity


def _to_long(wide):
    """Wide table (slot x station) -> one long column, in a fixed row order."""
    return wide.reset_index().melt(id_vars="slot")["value"].to_numpy()


def build_table(grid, capacity, horizon_slots=2):
    """One row per (slot, station) with features and the target.

    Target: `change` = bikes `horizon_slots` slots later minus bikes now.
    """
    df = grid.reset_index().melt(id_vars="slot", var_name="station_id", value_name="bikes_now")
    df["bikes_future"] = _to_long(grid.shift(-horizon_slots))

    # Recent movement: how much has the station changed in the last 15/30/60 minutes?
    for slots in (1, 2, 4):
        df[f"change_last_{slots * SLOT_MINUTES}min"] = df["bikes_now"] - _to_long(grid.shift(slots))

    df = df.dropna(subset=["bikes_now", "bikes_future"]).reset_index(drop=True)

    # Time features, in Dublin local time, for the moment being predicted
    t = (df["slot"] + pd.Timedelta(minutes=SLOT_MINUTES * horizon_slots)).dt.tz_convert("Europe/Dublin")
    df["hour"] = t.dt.hour
    df["minute"] = t.dt.minute
    df["dayofweek"] = t.dt.dayofweek

    df["capacity"] = df["station_id"].map(capacity)
    df["fill_ratio"] = df["bikes_now"] / df["capacity"]
    df["change"] = df["bikes_future"] - df["bikes_now"]
    return df