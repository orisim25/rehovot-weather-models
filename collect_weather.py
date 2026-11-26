import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import numpy as np

TZ = ZoneInfo("Asia/Jerusalem")
LAT, LON = 31.8928, 34.8113

BACKFILL_FILE = "backfill_stats.json"
HISTORY_FILE = "history.json"
LEADERBOARD_FILE = "leaderboard.json"

OPEN_METEO_MODELS = {
    "ECMWF": "ecmwf_ifs",
    "GFS": "gfs_seamless",
    "ICON": "icon_seamless",
}

LEAD_WEIGHTS = {1: 0.60, 3: 0.40}
RAIN_THRESHOLD_MM = 1.0
MIN_LIVE_DAYS = 14


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------
# 1. איסוף מדידת אמת יומית ממכון ויצמן (a108)
# ---------------------------------------------------------
def fetch_weizmann_today():
    """מושך את נתוני היום מתחנת מכון ויצמן (a108) ומחשב סיכום יומי."""
    url = "https://www.meteo.co.il/api/stations/a108/today"
    # הערה: אם ה-endpoint אצלך ב-IMS_Rehovot.js שונה, החלף את ה-URL כאן
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        # התאמה למבנה רשימת מדידות 10-דקתיות (או אובייקט יומי)
        if isinstance(data, list) and data:
            temps = [float(x["temp"]) for x in data if x.get("temp") is not None]
            winds = [float(x["wind_speed"]) for x in data if x.get("wind_speed") is not None]
            rains = [float(x["rain"]) for x in data if x.get("rain") is not None]
            return {
                "rain": round(sum(rains), 2) if rains else 0.0,
                "tmax": round(max(temps), 2) if temps else None,
                "tmin": round(min(temps), 2) if temps else None,
                "wind_avg": round(float(np.mean(winds)), 2) if winds else None,
            }
    except Exception as e:
        print(f"[WARN] Weizmann fetch failed: {e}")
    return None


# ---------------------------------------------------------
# 2. איסוף תחזיות +1d ו-+3d (Open-Meteo + IMS)
# ---------------------------------------------------------
def fetch_open_meteo_forecasts(today_dt):
    out = {}
    d1 = (today_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    d3 = (today_dt + timedelta(days=3)).strftime("%Y-%m-%d")

    for name, model_id in OPEN_METEO_MODELS.items():
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": LAT,
                    "longitude": LON,
                    "models": model_id,
                    "timezone": "Asia/Jerusalem",
                    "forecast_days": 5,
                    "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,wind_speed_10m_mean",
                },
                timeout=20,
            )
            r.raise_for_status()
            d = r.json().get("daily", {})
            times = d.get("time", [])
            model_fc = {}
            for target_date, lead_key in ((d1, "1d"), (d3, "3d")):
                if target_date in times:
                    idx = times.index(target_date)
                    model_fc[target_date] = {
                        "lead": lead_key,
                        "rain": d["precipitation_sum"]
