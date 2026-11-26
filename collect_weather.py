import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import urllib.request

TZ = ZoneInfo("Asia/Jerusalem")
LAT, LON = 31.8928, 34.8113
IMS_CITY_ID = "13"

HISTORY_FILE = "data/history.json"
STATS_FILE = "data/stats.json"
MODELS = ["IMS", "ECMWF", "GFS", "ICON"]

# ממוצע גשם חודשי רב-שנתי משוער ברחובות (סה"כ ~530 מ"מ בעונה)
MONTHLY_AVG_MM = {
    10: 25, 11: 75, 12: 130, 1: 140, 2: 100, 3: 45, 4: 15
}
SEASON_AVG_TOTAL = sum(MONTHLY_AVG_MM.values())


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8"))


def get_ims_forecast():
    res = fetch_json(f"https://ims.gov.il/he/forecast_data/{IMS_CITY_ID}")
    data = res.get("data", {})
    out = {}
    for date_str, val in data.items():
        if len(date_str) != 10:
            continue
        daily = val.get("daily", val)
        hourly = val.get("hourly", {})
        total_mm = float(daily.get("rain") or 0)
        if total_mm == 0 and isinstance(hourly, dict):
            total_mm = sum(float(h.get("rain") or 0) for h in hourly.values())
        out[date_str] = round(total_mm, 2)
    return out


def get_open_meteo_forecasts():
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        f"&daily=precipitation_sum"
        f"&models=ecmwf_ifs025,gfs_seamless,icon_seamless"
        f"&timezone=Asia%2FJerusalem&forecast_days=7"
    )
    res = fetch_json(url)
    daily = res.get("daily", {})
    dates = daily.get("time", [])

    mapping = {
        "ECMWF": "precipitation_sum_ecmwf_ifs025",
        "GFS": "precipitation_sum_gfs_seamless",
        "ICON": "precipitation_sum_icon_seamless",
    }

    out = {m: {} for m in mapping}
    for i, d in enumerate(dates):
        for model_name, key in mapping.items():
            vals = daily.get(key, [])
            if i < len(vals) and vals[i] is not None:
                out[model_name][d] = round(float(vals[i]), 2)
    return out


def get_actual_rain_yesterday(yesterday_str):
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        f"&daily=precipitation_sum&past_days=2&forecast_days=1"
        f"&timezone=Asia%2FJerusalem"
    )
    res = fetch_json(url)
    daily = res.get("daily", {})
    dates = daily.get("time", [])
    rains = daily.get("precipitation_sum", [])
    if yesterday_str in dates:
        idx = dates.index(yesterday_str)
        if rains[idx] is not None:
            return round(float(rains[idx]), 2)
    return None


def expected_season_avg_to_date(now):
    # מחשב כמה מ"מ בממוצע אמורים לרדת עד התאריך הנוכחי בעונת הגשם (אוקטובר-אפריל)
    order = [10, 11, 12, 1, 2, 3, 4]
    m = now.month
    if m in (5, 6, 7, 8, 9):
        return SEASON_AVG_TOTAL if m <= 8 else 0.0
    total = 0.0
    for mo in order:
        if mo == m:
            total += MONTHLY_AVG_MM[mo] * (now.day / 30.0)
            break
        total += MONTHLY_AVG_MM[mo]
    return round(total, 1)


def get_personality(bias_pct, rainy_days):
    if rainy_days < 3:
        return "מתחמם"
    if bias_pct > 20:
        return "מגזים"
    if bias_pct < -20:
        return "שמרן"
    return "צלף"


def get_disagreement(values):
    if not values or max(values) < 0.5:
        return {"level": "low", "label": "יבש • קונסנזוס"}
    spread = max(values) - min(values)
    avg = sum(values) / len(values)
    ratio = spread / max(avg, 1.0)
    if spread >= 8 or ratio >= 1.2:
        return {"level": "high", "label": "קרב פתוח"}
    if spread >= 3 or ratio >= 0.6:
        return {"level": "med", "label": "מחלוקת"}
    return {"level": "low", "label": "קונסנזוס"}


def main():
    os.makedirs("data", exist_ok=True)
    history = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)

    now = datetime.now(TZ)
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    forecasts = {"IMS": get_ims_forecast()}
    forecasts.update(get_open_meteo_forecasts())

    # שמירת Snapshots ל-24 שעות ו-72 שעות מראש
    for lead_days in (1, 3):
        target_date = (now + timedelta(days=lead_days)).strftime("%Y-%m-%d")
        lead_key = f"lead_{lead_days}d"
        history.setdefault(target_date, {"actual": None, "snapshots": {}})
        history[target_date]["snapshots"].setdefault(lead_key, {})
        for m in MODELS:
            if target_date in forecasts.get(m, {}):
                history[target_date]["snapshots"][lead_key][m] = forecasts[m][target_date]

    actual_yest = get_actual_rain_yesterday(yesterday_str)
    if actual_yest is not None:
        history.setdefault(yesterday_str, {"actual": None, "snapshots": {}})
        history[yesterday_str]["actual"] = actual_yest

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    # חישוב ליגת האלופות + אישיות + משקולות למודל של רחובות
    stats_m = {
        m: {"points": 0, "errors": [], "pred_sum": 0.0, "actual_sum": 0.0, "rainy_days": 0}
        for m in MODELS
    }
    season_rain = 0.0

    for d_str, entry in sorted(history.items()):
        actual = entry.get("actual")
        if actual is None:
            continue
        season_rain += actual
        snap = entry.get("snapshots", {}).get("lead_1d", {})
        if not snap:
            continue

        day_errs = {}
        for m in MODELS:
            if m in snap:
                pred = snap[m]
                err = abs(pred - actual)
                stats_m[m]["errors"].append(err)
                day_errs[m] = err
                if actual >= 1.0 or pred >= 1.0:
                    stats_m[m]["pred_sum"] += pred
                    stats_m[m]["actual_sum"] += actual
                    stats_m[m]["rainy_days"] += 1

        # חלוקת נקודות בימי גשם (>= 1 מ"מ בפועל או בתחזית ממוצעת)
        if actual >= 1.0 and len(day_errs) >= 2:
            ranked = sorted(day_errs.items(), key=lambda x: x[1])
            stats_m[ranked[0][0]]["points"] += 3
            stats_m[ranked[1][0]]["points"] += 1

    # בניית טבלת הליגה וחישוב משקולות
    leaderboard = []
    weights = {}
    for m in MODELS:
        s = stats_m[m]
        mae = round(sum(s["errors"]) / len(s["errors"]), 2) if s["errors"] else None
        bias_pct = 0.0
        if s["actual_sum"] > 0:
            bias_pct = round(((s["pred_sum"] - s["actual_sum"]) / s["actual_sum"]) * 100)
        personality = get_personality(bias_pct, s["rainy_days"])

        # משקל גבוה יותר לשגיאה נמוכה יותר (Inverse MAE)
        weights[m] = (1.0 / (mae + 0.5)) if (mae is not None and s["rainy_days"] >= 3) else 1.0

        leaderboard.append({
            "model": m,
            "points": s["points"],
            "mae_mm": mae,
            "bias_pct": bias_pct,
            "personality": personality
        })

    leaderboard.sort(key=lambda x: (-x["points"], x["mae_mm"] if x["mae_mm"] is not None else 999))

    w_sum = sum(weights.values())
    norm_weights = {m: weights[m] / w_sum for m in MODELS}

    # מציאת היום הגשום הקרוב ביותר (מתוך 5 הימים הקרובים)
    focus_day = None
    for i in range(5):
        d_str = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        vals = {m: forecasts.get(m, {}).get(d_str, 0.0) for m in MODELS}
        rehovot_mm = round(sum(vals[m] * norm_weights[m] for m in MODELS), 1)
        day_obj = {
            "date": d_str,
            "models": vals,
            "rehovot_model_mm": rehovot_mm,
            "disagreement": get_disagreement(list(vals.values()))
        }
        if focus_day is None or (max(vals.values()) >= 0.5 and max(focus_day["models"].values()) < 0.5):
            focus_day = day_obj

    expected_to_date = expected_season_avg_to_date(now)

    stats = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "focus_day": focus_day,
        "leaderboard": leaderboard,
        "season": {
            "actual_mm": round(season_rain, 1),
            "expected_to_date_mm": expected_to_date,
            "annual_avg_mm": SEASON_AVG_TOTAL,
            "pct_of_annual": round((season_rain / SEASON_AVG_TOTAL) * 100)
        }
    }

    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
