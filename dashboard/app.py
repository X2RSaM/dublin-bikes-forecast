"""Dublin Bikes live dashboard.

Run locally from the project folder:
    streamlit run dashboard/app.py

It calls the live prediction API on Google Cloud Run, so it needs no models or API key itself.
"""

import os

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "https://dublin-bikes-api-683472226763.europe-west1.run.app")
REPO_URL = "https://github.com/X2RSaM/dublin-bikes-forecast"
ALL = "All stations"

# Colours for each risk level (red / amber / green), as [R, G, B, opacity]
RISK_COLOURS = {"high": [214, 39, 40, 220], "medium": [245, 166, 35, 220], "low": [26, 152, 80, 200]}
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}

st.set_page_config(page_title="Dublin Bikes Forecast", page_icon="🚲", layout="wide")


@st.cache_data(ttl=300, show_spinner=False)
def load_forecast():
    """Ask the API for every station. Cached for 5 minutes, like the API's own refresh."""
    resp = requests.get(f"{API_URL}/forecast", timeout=90)  # long timeout: Cloud Run may be waking up
    resp.raise_for_status()
    df = pd.DataFrame(resp.json())
    df["risk_pct"] = (df["empty_risk"] * 100).round(0).astype(int)
    df["colour"] = df["empty_risk_level"].map(RISK_COLOURS)
    df["loaded_at"] = pd.Timestamp.now(tz="Europe/Dublin").strftime("%H:%M")
    return df


# ---- Header -------------------------------------------------------------------
st.title("🚲 Dublin Bikes: will there be a bike when you get there?")
st.caption("Live predictions for every station, 30 minutes ahead, from two LightGBM models "
           "trained on 12 months of Dublin Bikes data.")

with st.spinner("Getting live predictions (the first load can take up to a minute while the server wakes up)..."):
    try:
        df = load_forecast()
    except requests.RequestException:
        st.error("The prediction API isn't responding right now. Please try again in a minute.")
        st.stop()

info_col, button_col = st.columns([4, 1])
info_col.caption(f"Updated at {df['loaded_at'].iloc[0]} (Dublin time). Data refreshes every 5 minutes.")
if button_col.button("Refresh now"):
    load_forecast.clear()
    st.rerun()

# ---- Headline numbers ------------------------------------------------------
# Short labels so the four boxes fit on tablets; on phones Streamlit stacks them.
c1, c2, c3, c4 = st.columns(4)
c1.metric("Stations", len(df))
c2.metric("Bikes available", int(df["bikes_now"].sum()))
c3.metric("Empty now", int((df["bikes_now"] == 0).sum()))
c4.metric("High risk in 30 min", int((df["empty_risk_level"] == "high").sum()))

# ---- Station picker (controls the map) ---------------------------------------------
st.subheader("Find a station")
names = sorted(df["name"].dropna().unique())
choice = st.selectbox("Pick a station to zoom the map to it, or keep 'All stations' to see the city.",
                      [ALL] + names, index=0)

if choice == ALL:
    selected = None
    view = pdk.ViewState(latitude=df["lat"].mean(), longitude=df["lon"].mean(), zoom=12.5)
else:
    selected = df[df["name"] == choice].iloc[0]
    view = pdk.ViewState(latitude=selected["lat"], longitude=selected["lon"], zoom=15.5)

    d1, d2, d3 = st.columns(3)
    d1.metric("Bikes now", f"{selected['bikes_now']} of {selected['capacity']}")
    d2.metric("Expected in 30 min", f"{selected['bikes_in_30_min']:.0f}",
              delta=f"{selected['bikes_in_30_min'] - selected['bikes_now']:+.1f}")
    d3.metric("Chance of being empty", f"{selected['risk_pct']}%",
              help=f"Risk level: {selected['empty_risk_level']}")

# ---- Map ----------------------------------------------------------------------
st.markdown("🔴 **High** &nbsp; 🟠 **Medium** &nbsp; 🟢 **Low** risk of having no bikes in 30 minutes. "
            "Hover over (or tap) a station for details.")

layers = [pdk.Layer(
    "ScatterplotLayer",
    data=df,
    get_position="[lon, lat]",
    get_fill_color="colour",
    get_radius=40,              # about 40 m on the ground...
    radius_min_pixels=6,        # ...but never smaller than 6 pixels
    radius_max_pixels=12,       # ...or bigger than 12 pixels, whatever the zoom
    pickable=True,
    stroked=True,
    get_line_color=[255, 255, 255],
    line_width_min_pixels=1,
)]
if selected is not None:
    # A white ring around the chosen station so it stands out
    layers.append(pdk.Layer(
        "ScatterplotLayer",
        data=pd.DataFrame([selected]),
        get_position="[lon, lat]",
        get_radius=60,
        radius_min_pixels=16,
        radius_max_pixels=20,
        filled=False,
        stroked=True,
        get_line_color=[255, 255, 255],
        line_width_min_pixels=3,
    ))

tooltip = {
    "html": "<b>{name}</b><br/>Bikes now: {bikes_now} of {capacity}<br/>"
            "In 30 min: about {bikes_in_30_min}<br/>Chance of being empty: {risk_pct}%",
    "style": {"fontSize": "13px"},
}
# The key changes with the chosen station, so the map re-centres when the choice changes
st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view, tooltip=tooltip, map_style=None),
                height=480, width="stretch", key=f"map-{choice}")

# ---- Stations to watch ----------------------------------------------------------------
st.subheader("Stations most likely to have no bikes in 30 minutes")
watch = (df.assign(order=df["empty_risk_level"].map(RISK_ORDER))
         .query("empty_risk_level != 'low'")
         .sort_values(["order", "empty_risk"], ascending=[True, False]))
if watch.empty:
    st.success("No stations are at medium or high risk right now.")
else:
    st.dataframe(
        watch[["name", "bikes_now", "bikes_in_30_min", "risk_pct", "empty_risk_level"]]
        .rename(columns={"name": "Station", "bikes_now": "Bikes now", "bikes_in_30_min": "In 30 min",
                         "risk_pct": "Chance empty (%)", "empty_risk_level": "Risk"}),
        hide_index=True, width="stretch",
    )

# ---- How to read this ------------------------------------------------------------
with st.expander("How to read these predictions"):
    st.markdown(
        "- **Bikes in 30 minutes** comes from a LightGBM model with a mean error of about 1 bike "
        "on three months of unseen data, beating every simple baseline.\n"
        "- **Chance of being empty** comes from a second LightGBM model. A *high* warning for a "
        "station that still has bikes was right about **46%** of the time in testing, about "
        "18× better than chance, but not a guarantee. Treat it as a risk, not a promise.\n"
        "- Predictions use live data from the JCDecaux API and are served by a FastAPI service "
        "on Google Cloud Run."
    )

st.caption(f"[Source code and full results on GitHub]({REPO_URL}) · "
           f"[Prediction API]({API_URL}/docs) · "
           "Data: Dublin City Council / JCDecaux, CC BY 4.0")