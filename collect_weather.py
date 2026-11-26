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
# 1. איסוף מדידת אמת יומית ממכון ויצמן (a108h24)
# ---------------------------------------------------------
def fetch_weizmann_daily(date_str):
    """מושך סיכום יומי רשמי לתחנת מכון ויצמן (a108h24) ממשרד החקלאות."""
    url = f"https://uny9pigfrl.execute-api.eu-north-1.amazonaws.com/Observation/daily_summary/get-data?date={date_str}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json().get("dailySummaryFinalData", {})
        st = data.get("a108h24")
        if not st:
            return None

        def val(key):
            item = st.get(key)
            v = item.get("value") if isinstance(item, dict) else item
            return round(float(v), 2) if v is not None else None

        rain_val = val("SumPrec")
        return {
            "rain": rain_val if rain_val is not None else 0.0,
            "tmax": val("MaxT"),
            "tmin": val("MinT"),
            "wind_avg": val("AvgWS"),
        }
    except Exception as e:
        print(f"[WARN] Weizmann daily fetch failed for {date_str}: {e}")
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
                        "rain": d["precipitation_sum"][idx],
                        "tmax": d["temperature_2m_max"][idx],
                        "tmin": d["temperature_2m_min"][idx],
                        "wind_avg": d["wind_speed_10m_mean"][idx],
                    }
            out[name] = model_fc
        except Exception as e:
            print(f"[WARN] {name} forecast failed: {e}")
    return out


def fetch_ims_forecast(today_dt):
    """מושך תחזית מהשמ״ט (רחובות - תחנה 13) עבור +1d ו-+3d."""
    d1 = (today_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    d3 = (today_dt + timedelta(days=3)).strftime("%Y-%m-%d")
    model_fc = {}
    try:
        r = requests.get("https://ims.gov.il/he/forecast_data/13", timeout=20)
        r.raise_for_status()
        days_data = r.json().get("data", {})
        for target_date, lead_key in ((d1, "1d"), (d3, "3d")):
            day_obj = days_data.get(target_date)
            if not day_obj:
                continue
            daily = day_obj.get("daily", {})
            hourly = day_obj.get("hourly", {})
            rains = [float(h.get("rain", 0) or 0) for h in hourly.values()] if isinstance(hourly, dict) else []
            winds = [float(h["wind_speed"]) for h in hourly.values() if h.get("wind_speed") is not None] if isinstance(hourly, dict) else []

            model_fc[target_date] = {
                "lead": lead_key,
                "rain": round(sum(rains), 2) if rains else float(daily.get("rain", 0) or 0),
                "tmax": float(daily["maximum_temperature"]) if daily.get("maximum_temperature") is not None else None,
                "tmin": float(daily["minimum_temperature"]) if daily.get("minimum_temperature") is not None else None,
                "wind_avg": round(float(np.mean(winds)), 2) if winds else None,
            }
    except Exception as e:
        print(f"[WARN] IMS forecast failed: {e}")
    return model_fc


# ---------------------------------------------------------
# 3. חישוב ציוני WMO (זהה ל-Backfill) ומיזוג
# ---------------------------------------------------------
def rmse(a, b):
    return float(np.sqrt(np.mean((a - b) ** 2)))


def crmse(obs, fc):
    bias = np.mean(fc - obs)
    return float(np.sqrt(np.mean(((fc - bias) - obs) ** 2)))


def skill_score(err_mod, err_ref):
    if err_ref == 0 or np.isnan(err_mod) or np.isnan(err_ref):
        return 0.0
    return max(0.0, min(100.0, (1.0 - err_mod / err_ref) * 100.0))


def score_live_model(history, model_name):
    dates = sorted(history.keys())
    lead_scores = {}

    for lead in (1, 3):
        lead_key = f"{lead}d"
        r_obs, r_fc, r_pers = [], [], []
        tmax_obs, tmax_fc, tmax_pers = [], [], []
        tmin_obs, tmin_fc, tmin_pers = [], [], []
        w_obs, w_fc, w_pers = [], [], []

        for i, d_str in enumerate(dates):
            if i < lead:
                continue
            obs = history[d_str].get("obs")
            pers = history[dates[i - lead]].get("obs")
            fc = history[d_str].get("forecasts", {}).get(model_name, {}).get(lead_key)
            if not obs or not pers or not fc:
                continue

            if None not in (obs.get("rain"), pers.get("rain"), fc.get("rain")):
                r_obs.append(obs["rain"]); r_fc.append(fc["rain"]); r_pers.append(pers["rain"])
            if None not in (obs.get("tmax"), obs.get("tmin"), pers.get("tmax"), pers.get("tmin"), fc.get("tmax"), fc.get("tmin")):
                tmax_obs.append(obs["tmax"]); tmax_fc.append(fc["tmax"]); tmax_pers.append(pers["tmax"])
                tmin_obs.append(obs["tmin"]); tmin_fc.append(fc["tmin"]); tmin_pers.append(pers["tmin"])
            if None not in (obs.get("wind_avg"), pers.get("wind_avg"), fc.get("wind_avg")):
                w_obs.append(obs["wind_avg"]); w_fc.append(fc["wind_avg"]); w_pers.append(pers["wind_avg"])

        if len(tmax_obs) < MIN_LIVE_DAYS:
            return None, len(tmax_obs)

        ro, rf, rp = np.array(r_obs), np.array(r_fc), np.array(r_pers)
        hits = int(((ro >= RAIN_THRESHOLD_MM) & (rf >= RAIN_THRESHOLD_MM)).sum())
        misses = int(((ro >= RAIN_THRESHOLD_MM) & (rf < RAIN_THRESHOLD_MM)).sum())
        fa = int(((ro < RAIN_THRESHOLD_MM) & (rf >= RAIN_THRESHOLD_MM)).sum())
        csi = (hits / (hits + misses + fa) * 100.0) if (hits + misses + fa) > 0 else 50.0
        rmsle_s = skill_score(rmse(np.log1p(ro), np.log1p(np.clip(rf, 0, None))), rmse(np.log1p(ro), np.log1p(np.clip(rp, 0, None))))
        rain_s = 0.5 * csi + 0.5 * rmsle_s

        t_max_s = skill_score(crmse(np.array(tmax_obs), np.array(tmax_fc)), rmse(np.array(tmax_obs), np.array(tmax_pers)))
        t_min_s = skill_score(crmse(np.array(tmin_obs), np.array(tmin_fc)), rmse(np.array(tmin_obs), np.array(tmin_pers)))
        temp_s = (t_max_s + t_min_s) / 2.0

        wind_s = skill_score(crmse(np.array(w_obs), np.array(w_fc)), rmse(np.array(w_obs), np.array(w_pers))) if len(w_obs) >= MIN_LIVE_DAYS else 0.0

        lead_scores[lead] = {"rain": rain_s, "temp": temp_s, "wind": wind_s, "days": len(tmax_obs)}

    rain_total = round(LEAD_WEIGHTS[1] * lead_scores[1]["rain"] + LEAD_WEIGHTS[3] * lead_scores[3]["rain"], 1)
    temp_total = round(LEAD_WEIGHTS[1] * lead_scores[1]["temp"] + LEAD_WEIGHTS[3] * lead_scores[3]["temp"], 1)
    wind_total = round(LEAD_WEIGHTS[1] * lead_scores[1]["wind"] + LEAD_WEIGHTS[3] * lead_scores[3]["wind"], 1)
    overall = round(0.45 * rain_total + 0.35 * temp_total + 0.20 * wind_total, 1)

    return {"rain": rain_total, "temp": temp_total, "wind": wind_total, "overall": overall}, lead_scores[1]["days"]


def build_leaderboard(backfill, history, today_str):
    models_out = {}
    all_names = ["ECMWF", "GFS", "ICON", "IMS"]

    for name in all_names:
        bf = backfill.get("models", {}).get(name)
        live, live_days = score_live_model(history, name)

        if bf and live:
            bf_days = bf["days_evaluated"]
            tot = bf_days + live_days
            rain_s = round((bf["rain"]["score"] * bf_days + live["rain"] * live_days) / tot, 1)
            temp_s = round((bf["temp"]["score"] * bf_days + live["temp"] * live_days) / tot, 1)
            wind_s = round((bf["wind"]["score"] * bf_days + live["wind"] * live_days) / tot, 1)
            overall = round(0.45 * rain_s + 0.35 * temp_s + 0.20 * wind_s, 1)
            models_out[name] = {"status": "active", "days": tot, "overall": overall, "rain": rain_s, "temp": temp_s, "wind": wind_s}
        elif bf:
            models_out[name] = {
                "status": "active",
                "days": bf["days_evaluated"],
                "overall": bf["overall_score"],
                "rain": bf["rain"]["score"],
                "temp": bf["temp"]["score"],
                "wind": bf["wind"]["score"],
            }
        elif live:
            models_out[name] = {"status": "active", "days": live_days, **live}
        else:
            models_out[name] = {"status": "collecting", "days": live_days, "min_days": MIN_LIVE_DAYS}

    active = {k: v for k, v in models_out.items() if v["status"] == "active"}
    leaders = {
        "overall": max(active, key=lambda k: active[k]["overall"]),
        "rain": max(active, key=lambda k: active[k]["rain"]),
        "temp": max(active, key=lambda k: active[k]["temp"]),
        "wind": max(active, key=lambda k: active[k]["wind"]),
    }

    return {
        "updated_at": today_str,
        "station": "מכון ויצמן (a108)",
        "leaders": leaders,
        "models": models_out,
    }


def main():
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")

    backfill = load_json(BACKFILL_FILE, {})
    history = load_json(HISTORY_FILE, {})

    # 1. שמירת מדידת אמת (אתמול אם חסר + היום אם כבר פורסם)
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for d_str in (yesterday_str, today_str):
        if not history.get(d_str, {}).get("obs"):
            obs = fetch_weizmann_daily(d_str)
            if obs and obs.get("tmax") is not None:
                history.setdefault(d_str, {})["obs"] = obs
                print(f"Saved Weizmann obs for {d_str}: {obs}")

    # 2. שמירת תחזיות +1d ו-+3d לכל המודלים
    all_fc = fetch_open_meteo_forecasts(now)
    ims_fc = fetch_ims_forecast(now)
    if ims_fc:
        all_fc["IMS"] = ims_fc

    for model_name, targets in all_fc.items():
        for target_date, payload in targets.items():
            lead_key = payload.pop("lead")
            history.setdefault(target_date, {}).setdefault("forecasts", {}).setdefault(model_name, {})[lead_key] = payload

    save_json(HISTORY_FILE, history)

    # 3. בניית קובץ ה-Leaderboard לווידג׳ט
    lb = build_leaderboard(backfill, history, today_str)
    save_json(LEADERBOARD_FILE, lb)
    print(f"Updated {LEADERBOARD_FILE}. Leaders: {lb['leaders']}")


if __name__ == "__main__":
    main()
