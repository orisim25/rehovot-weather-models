// תחליף לכתובת ה-Raw של הריפו שלך ב-GitHub
const STATS_URL = "https://raw.githubusercontent.com/USERNAME/REPO/main/data/stats.json";

const LAT = 31.8928;
const LON = 34.8113;

function getSunTime(date, lat, lon, isSunrise) {
  const rad = Math.PI / 180;
  const deg = 180 / Math.PI;
  const start = new Date(date.getFullYear(), 0, 0);
  const N = Math.floor((date - start) / 86400000);
  const lngHour = lon / 15;
  const t = N + (((isSunrise ? 6 : 18) - lngHour) / 24);
  const M = (0.9856 * t) - 3.289;
  let L = (M + (1.916 * Math.sin(M * rad)) + (0.020 * Math.sin(2 * M * rad)) + 282.634) % 360;
  if (L < 0) L += 360;
  let RA = (deg * Math.atan(0.91764 * Math.tan(L * rad))) % 360;
  if (RA < 0) RA += 360;
  RA = (RA + (Math.floor(L / 90) * 90 - Math.floor(RA / 90) * 90)) / 15;
  const sinDec = 0.39782 * Math.sin(L * rad);
  const cosDec = Math.cos(Math.asin(sinDec));
  const cosH = (Math.cos(90.833 * rad) - (sinDec * Math.sin(lat * rad))) / (cosDec * Math.cos(lat * rad));
  const H = (isSunrise ? (360 - deg * Math.acos(cosH)) : (deg * Math.acos(cosH))) / 15;
  const T = H + RA - (0.06571 * t) - 6.622;
  let UT = (T - lngHour) % 24;
  if (UT < 0) UT += 24;
  let local = (UT - (date.getTimezoneOffset() / 60)) % 24;
  if (local < 0) local += 24;
  const hh = Math.floor(local);
  const mm = Math.round((local - hh) * 60);
  return hh * 60 + mm;
}

function formatShortDate(dateStr) {
  const d = new Date(dateStr + "T12:00:00");
  const days = ["א׳", "ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "ש׳"];
  return `יום ${days[d.getDay()]} (${d.getDate()}.${d.getMonth() + 1})`;
}

function drawSeasonBar(actual, expected, total) {
  const w = 310, h = 10;
  const dc = new DrawContext();
  dc.size = new Size(w, h);
  dc.opaque = false;
  dc.respectScreenScale = true;

  const track = new Path();
  track.addRoundedRect(new Rect(0, 2, w, 6), 3, 3);
  dc.addPath(track);
  dc.setFillColor(new Color("#FFFFFF", 0.15));
  dc.fillPath();

  const actW = Math.min((actual / total) * w, w);
  if (actW > 0) {
    const fill = new Path();
    fill.addRoundedRect(new Rect(0, 2, Math.max(actW, 6), 6), 3, 3);
    dc.addPath(fill);
    dc.setFillColor(new Color("#38BDF8"));
    dc.fillPath();
  }

  // סימון הממוצע הצפוי עד היום
  const expX = Math.min(Math.max((expected / total) * w, 2), w - 2);
  const marker = new Path();
  marker.move(new Point(expX, 0));
  marker.addLine(new Point(expX, h));
  dc.addPath(marker);
  dc.setStrokeColor(new Color("#FFD166"));
  dc.setLineWidth(2);
  dc.strokePath();

  return dc.getImage();
}

const now = new Date();
const mins = now.getHours() * 60 + now.getMinutes();
const isNight = mins < getSunTime(now, LAT, LON, true) || mins >= getSunTime(now, LAT, LON, false);

let data = null;
try {
  const req = new Request(STATS_URL);
  req.timeoutInterval = 10;
  data = await req.loadJSON();
} catch (e) {}

const widget = new ListWidget();
widget.setPadding(10, 14, 10, 14);
const grad = new LinearGradient();
grad.locations = [0, 1];
grad.colors = isNight
  ? [new Color("#141E30"), new Color("#243B55")]
  : [new Color("#1E62B0"), new Color("#4A90E2")];
widget.backgroundGradient = grad;

if (!data) {
  const err = widget.addText("ממתין לנתוני stats.json מ-GitHub...");
  err.font = Font.mediumSystemFont(12);
  err.textColor = Color.white();
} else {
  const focus = data.focus_day;
  const dis = focus.disagreement;
  const badgeColor = dis.level === "high" ? "#EF4444" : dis.level === "med" ? "#F59E0B" : "#10B981";

  // 1. כותרת: המודל של רחובות + מדד אי-ההסכמה
  const top = widget.addStack();
  top.layoutHorizontally();
  top.centerAlignContent();

  const pill = top.addStack();
  pill.backgroundColor = new Color(badgeColor, 0.9);
  pill.cornerRadius = 5;
  pill.setPadding(2, 6, 2, 6);
  const pillTxt = pill.addText(dis.label);
  pillTxt.font = Font.boldSystemFont(9.5);
  pillTxt.textColor = Color.white();

  top.addSpacer();

  const title = top.addText(`מודל רחובות: ${focus.rehovot_model_mm} מ״מ • ${formatShortDate(focus.date)}`);
  title.font = Font.boldSystemFont(12);
  title.textColor = Color.white();

  widget.addSpacer(6);

  // 2. ליגת האלופות + אישיות + תחזית ליום הקרוב (4 עמודות)
  const grid = widget.addStack();
  grid.layoutHorizontally();

  data.leaderboard.forEach((m, idx) => {
    const col = grid.addStack();
    col.layoutVertically();
    col.centerAlignContent();
    col.spacing = 2;
    col.setPadding(4, 4, 4, 4);
    col.cornerRadius = 7;
    col.backgroundColor = new Color("#FFFFFF", idx === 0 ? 0.16 : 0.07);

    const name = col.addText(idx === 0 ? `🏆 ${m.model}` : m.model);
    name.font = Font.boldSystemFont(10.5);
    name.textColor = Color.white();

    const predMm = focus.models[m.model] ?? 0;
    const mmTxt = col.addText(`${predMm.toFixed(1)} מ״מ`);
    mmTxt.font = Font.boldSystemFont(11);
    mmTxt.textColor = predMm > 0 ? new Color("#38BDF8") : new Color("#FFFFFF", 0.7);

    const meta = col.addText(`${m.points} נק׳ • ${m.personality}`);
    meta.font = Font.mediumSystemFont(8.5);
    meta.textColor = new Color("#FFFFFF", 0.78);

    if (idx < data.leaderboard.length - 1) grid.addSpacer(5);
  });

  widget.addSpacer(8);

  // 3. מרוץ הגשם העונתי ברחובות
  const s = data.season;
  const sHeader = widget.addStack();
  sHeader.layoutHorizontally();
  sHeader.centerAlignContent();

  const sRight = sHeader.addText(`ממוצע להיום: ${s.expected_to_date_mm} מ״מ`);
  sRight.font = Font.mediumSystemFont(9.5);
  sRight.textColor = new Color("#FFD166");

  sHeader.addSpacer();

  const sLeft = sHeader.addText(`מרוץ הגשם: ${s.actual_mm} / ${s.annual_avg_mm} מ״מ (${s.pct_of_annual}%)`);
  sLeft.font = Font.semiboldSystemFont(10);
  sLeft.textColor = Color.white();

  widget.addSpacer(4);
  const barImg = drawSeasonBar(s.actual_mm, s.expected_to_date_mm, s.annual_avg_mm);
  const barEl = widget.addImage(barImg);
  barEl.resizable = true;
}

Script.setWidget(widget);
if (!config.runsInWidget) {
  await widget.presentMedium();
}
Script.complete();
