# ======= EMBEDDED BLOCKS (auto-managed by Telegram commands) ===============
# ==== EMBEDDED_CONFIG (JSON) START
EMBEDDED_CONFIG = r"""{
  "server": {
    "port": 5320
  },
  "frame": {
    "tz": "Asia/Seoul",
    "ical_url": "https://calendar.google.com/calendar/ical/bob.gondrae%40gmail.com/private-00822d9dbbe3140b9253bf2e0bda95c6/basic.ics"
  },
  "weather": {
    "provider": "openweathermap",
    "api_key": "9809664c22a3501382380f2781e1a9da",
    "location": "Seoul, South Korea",
    "units": "metric"
  },
  "telegram": {
    "bot_token": "8203763129:AAH3AUckwP5nY-SZ9aVd-F6Rh6Jakb145SA",
    "allowed_user_ids": [5517670242],
    "mode": "polling",
    "webhook_base": "",
    "path_secret": ""
  },
  "google": {
    "scopes": [
      "https://www.googleapis.com/auth/calendar"
    ],
    "calendar": {
      "id": "bob.gondrae@gmail.com"
    }
  },
  "todoist": {
    "api_token": "0aa4d2a4f95e952a1f635c14d6c6ba7e3b26bc2b",
    "max_items": 20
  },
  "bus": {
    "city_code": "",
    "node_id": "",
    "key": "3d3d725df7c8daa3445ada3ceb7778d94328541e6eb616f02c0b82cb11ff182f"
  }
}"""
# ==== EMBEDDED_CONFIG (JSON) END

# ==== EMBEDDED_VERSES START
EMBEDDED_VERSES = r"""테스트"""
# ==== EMBEDDED_VERSES END
# ===========================================================================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fully-integrated Smart Frame + Telegram bot with Google OAuth routes
(UI restores sframe's older 'Monthly Calendar + Photo Fade' layout)

* Todoist: fetch tasks and render in 2 columns (10 items each, total 20)
* Verse: /set -> verse input that shows on board
* Bot duplication guard (file lock) to avoid double polling
* Bus: nationwide arrival info via TAGO API with Telegram configuration
"""

# Code below is organized with clearly marked sections.
# Search for lines like `# === [SECTION: ...] ===` to navigate.

# === [SECTION: Imports / Standard & Third-party] ==============================
import os, json, time, secrets, threading, collections, re, socket, fcntl, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import html
import logging
from flask import Flask, request, jsonify, render_template_string, abort, send_from_directory, redirect, url_for, make_response
from werkzeug.middleware.proxy_fix import ProxyFix
import telebot
from functools import wraps

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bus")
# ======= Embedded-block helpers (final) ======================================
import tempfile, os, json
CFG_START = "# ==== EMBEDDED_CONFIG (JSON) START"
CFG_END   = "# ==== EMBEDDED_CONFIG (JSON) END"
VER_START = "# ==== EMBEDDED_VERSES START"
VER_END   = "# ==== EMBEDDED_VERSES END"

def _extract_block(src_text: str, start_tag: str, end_tag: str):
    s = src_text.find(start_tag); e = src_text.find(end_tag)
    if s == -1 or e == -1 or e <= s:
        raise RuntimeError(f"Marker not found: {start_tag}..{end_tag}")
    s_body = src_text.find("\n", s) + 1
    e_body = e
    return s_body, e_body, src_text[s_body:e_body]

def _replace_block_in_text(src_text: str, start_tag: str, end_tag: str, new_body: str) -> str:
    s_body, e_body, _old = _extract_block(src_text, start_tag, end_tag)
    if not new_body.endswith("\n"): new_body += "\n"
    return src_text[:s_body] + new_body + src_text[e_body:]

def _atomic_write(path: str, data: str):
    d = os.path.dirname(os.path.abspath(path)) or "."
    with tempfile.NamedTemporaryFile("w", delete=False, dir=d, encoding="utf-8") as tmp:
        tmp.write(data); tmp.flush(); os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def _read_block(start_tag: str, end_tag: str, file_path: str = __file__) -> str:
    with open(file_path, "r", encoding="utf-8") as f:
        src = f.read()
    _, _, body = _extract_block(src, start_tag, end_tag)
    # If wrapped as VAR = r"""...""", return only the inner text
    import re as _re
    m = _re.search(r'r?"""\s*([\s\S]*?)\s*"""', body)
    return (m.group(1) if m else body)

def _write_block(new_text: str, start_tag: str, end_tag: str, file_path: str = __file__):
    with open(file_path, "r", encoding="utf-8") as f:
        src = f.read()
    varname = "EMBEDDED_CONFIG" if "CONFIG" in start_tag else "EMBEDDED_VERSES"
    wrapped = varname + ' = r"""' + new_text + '"""' 
    _atomic_write(file_path, _replace_block_in_text(src, start_tag, end_tag, wrapped))

def load_config_from_embedded(defaults: dict):
    data = json.loads(_read_block(CFG_START, CFG_END) or "{}")
    def deep_fill(dst, src):
        for k, v in src.items():
            if k not in dst:
                dst[k] = v
            elif isinstance(v, dict):
                dst[k] = deep_fill(dst.get(k, {}) or {}, v)
        return dst
    return deep_fill(data, defaults)

def save_config_to_source(new_data: dict, file_path: str = __file__):
    json_text = json.dumps(new_data, ensure_ascii=False, indent=2)
    _write_block(json_text, CFG_START, CFG_END, file_path=file_path)

def get_verse() -> str:
    return _read_block(VER_START, VER_END).strip()

def set_verse(text: str):
    _write_block((text or "").strip(), VER_START, VER_END)
# ===========================================================================

# === [SECTION: Optional Google libraries (lazy check)] =======================
# - 구글 라이브러리가 없을 수 있으므로 임포트 시도 후 플래그만 세팅
try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import Flow
    GOOGLE_OK = True
except Exception:
    GOOGLE_OK = False

# === [SECTION: Paths / Base config file locations] ===========================
BASE = Path("/root/scal")
STATE_PATH = BASE / "sframe_state.json"
PHOTOS_DIR = Path("/root/rsf/photo")
GCLIENT_PATH = BASE / "google_client_secret.json"
GTOKEN_PATH = BASE / "google_token.json"
BASE.mkdir(parents=True, exist_ok=True)
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

# === [SECTION: Default configuration structure] ==============================
DEFAULT_CFG = {
    "server": {"port": 5320},
    "frame": {"tz": "Asia/Seoul", "ical_url": ""},
    "weather": {
        "provider": "openweathermap",
        "api_key": "",
        "location": "Seoul, South Korea",
        "units": "metric"
    },
    "telegram": {
        "bot_token": "",
        "allowed_user_ids": [],            # e.g. [5517670242]
        "mode": "polling",                 # polling | webhook
        "webhook_base": "",
        "path_secret": ""
    },
    "google": {
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
        "calendar": {"id": "primary"}

},
    # Todoist (config에서 설정) — 여기 값은 기본값
    "todoist": {
        "api_token": "",                   # 설정에 넣은 토큰 사용; 비어있으면 비활성
        "filter": "today | overdue",       # Todoist filter query
        "project_id": "",                  # optional: limit to project
        "max_items": 20                    # UI는 좌10/우10
    },
    # Bus 설정
    "bus": {
        "city_code": "",
        "node_id": "",
        "key": "",
    }
}
CFG = load_config_from_embedded(DEFAULT_CFG)

# === [SECTION: Timezone utilities] ===========================================
TZ = timezone(timedelta(hours=9)) if CFG["frame"]["tz"] == "Asia/Seoul" else timezone.utc
TZ_NAME = "Asia/Seoul" if CFG["frame"]["tz"] == "Asia/Seoul" else "UTC"

# === [SECTION: iCal loader (with basic fallback parser)] =====================
_ical_cache = {"url": None, "ts": 0.0, "events": []}

def _fmt_ics_date(v: str) -> str:
    if not v:
        return ""
    v = v.strip()
    if len(v) >= 8 and v[:8].isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v

def _parse_ics_basic(text: str):
    """Very basic ICS event parser without external libs."""
    evs, cur = [], {}
    for raw in text.splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line.startswith("SUMMARY:"):
            cur["title"] = line[8:].strip()
        elif line.startswith("DTSTART"):
            cur["start"] = _fmt_ics_date(line.split(":", 1)[1])
        elif line.startswith("DTEND"):
            cur["end"] = _fmt_ics_date(line.split(":", 1)[1])
        elif line == "END:VEVENT":
            if "start" in cur:
                cur.setdefault("end", cur["start"])
                cur.setdefault("title", "(untitled)")
                evs.append(cur)
    return evs

def fetch_ical(url: str):
    """Fetch ICS; use python-ics if available else fallback parser."""
    global _ical_cache
    now = time.time()
    if not url:
        return []
    if _ical_cache["url"] == url and now - _ical_cache["ts"] < 300:
        return _ical_cache["events"]
    r = requests.get(url, timeout=10); r.raise_for_status()
    text = r.text
    try:
        from ics import Calendar
        cal = Calendar(text)
        evs = []
        for ev in cal.events:
            start = ev.begin.date().isoformat() if getattr(ev, "begin", None) else ""
            end = ev.end.date().isoformat() if getattr(ev, "end", None) else start
            title = (ev.name or "").strip() or "(untitled)"
            evs.append({"title": title, "start": start, "end": end})
    except Exception:
        evs = _parse_ics_basic(text)
    evs.sort(key=lambda x: (x.get("start", ""), x.get("title", "")))
    _ical_cache = {"url": url, "ts": now, "events": evs}
    return evs

def month_filter(items, y, m):
    mm = f"{y:04d}-{m:02d}"
    return [e for e in items if (e.get("start", "").startswith(mm) or e.get("end", "").startswith(mm))]

# === [SECTION: Weather (OpenWeatherMap API)] =================================
_weather_cache = {"key": "", "loc": "", "ts": 0.0, "data": None}
_air_cache = {"key": "", "loc": "", "ts": 0.0, "data": None}

def _owm_geocode(q, key):
    url = "https://api.openweathermap.org/geo/1.0/direct"
    r = requests.get(url, params={"q": q, "limit": 1, "appid": key}, timeout=10)
    r.raise_for_status()
    arr = r.json()
    if not arr:
        raise RuntimeError("Location not found")
    return float(arr[0]["lat"]), float(arr[0]["lon"])

def _owm_fetch_onecall(lat, lon, key, units):
    r = requests.get(
        "https://api.openweathermap.org/data/3.0/onecall",
        params={"lat": lat, "lon": lon, "appid": key, "units": units, "exclude": "minutely,hourly,alerts"},
        timeout=10,
    )
    r.raise_for_status()
    js = r.json()
    def icon_url(code): return f"https://openweathermap.org/img/wn/{code}@2x.png"
    cur = js.get("current", {})
    dailies = (js.get("daily") or [])[:5]
    cur_data = {"temp": round(cur.get("temp", 0)), "icon": icon_url(cur.get("weather", [{}])[0].get("icon", "01d"))}
    days = []
    for d in dailies:
        dt = datetime.fromtimestamp(int(d.get("dt", 0)), tz=timezone.utc).astimezone(TZ).date()
        t = d.get("temp", {})
        icon = (d.get("weather", [{}])[0] or {}).get("icon", "01d")
        days.append({"date": dt.isoformat(), "min": round(t.get("min", 0)), "max": round(t.get("max", 0)), "icon": icon_url(icon)})
    return {"current": cur_data, "days": days}

def _owm_fetch_fiveday(lat, lon, key, units):
    cur = requests.get("https://api.openweathermap.org/data/2.5/weather",
                       params={"lat": lat, "lon": lon, "appid": key, "units": units}, timeout=10).json()
    fc = requests.get("https://api.openweathermap.org/data/2.5/forecast",
                      params={"lat": lat, "lon": lon, "appid": key, "units": units}, timeout=10).json()
    def icon_url(code): return f"https://openweathermap.org/img/wn/{code}@2x.png"
    cur_data = {"temp": round(cur.get("main", {}).get("temp", 0)), "icon": icon_url(cur.get("weather", [{}])[0].get("icon", "01d"))}
    by_day = collections.defaultdict(list)
    for it in fc.get("list", []):
        ts = int(it.get("dt", 0))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ).date()
        by_day[dt].append(it)
    days = []
    for d in sorted(by_day.keys())[:5]:
        arr = by_day[d]
        tmins, tmaxs, icons = [], [], []
        for it in arr:
            m = it.get("main", {})
            tmins.append(m.get("temp_min"))
            tmaxs.append(m.get("temp_max"))
            icons.append(it.get("weather", [{}])[0].get("icon", "01d"))
        pick = None
        for it in arr:
            hour = datetime.fromtimestamp(int(it["dt"]), tz=timezone.utc).astimezone(TZ).hour
            if 9 <= hour <= 15:
                pick = it.get("weather", [{}])[0].get("icon", "01d"); break
        if not pick:
            pick = max(set(icons), key=icons.count)
        days.append({"date": d.isoformat(), "min": round(min(tmins)), "max": round(max(tmaxs)), "icon": icon_url(pick)})
    return {"current": cur_data, "days": days}

def fetch_weather():
    cfgw = CFG.get("weather", {})
    key = cfgw.get("api_key", "").strip()
    loc = cfgw.get("location", "").strip()
    units = cfgw.get("units", "metric")
    if not key or not loc:
        return None
    now = time.time()
    cache_ok = (_weather_cache["data"] is not None and
                _weather_cache["key"] == key and
                _weather_cache["loc"] == loc and
                now - _weather_cache["ts"] < 600)
    if cache_ok:
        return _weather_cache["data"]
    lat, lon = _owm_geocode(loc, key)
    try:
        data = _owm_fetch_onecall(lat, lon, key, units)
    except Exception:
        data = _owm_fetch_fiveday(lat, lon, key, units)
    _weather_cache.update({"key": key, "loc": loc, "ts": now, "data": data})
    return data

# --- Air quality ------------------------------------------------------------
def fetch_air_quality():
    cfgw = CFG.get("weather", {})
    key = cfgw.get("api_key", "").strip()
    loc = cfgw.get("location", "").strip()
    if not key or not loc:
        return None
    now = time.time()
    cache_ok = (
        _air_cache["data"] is not None
        and _air_cache["key"] == key
        and _air_cache["loc"] == loc
        and now - _air_cache["ts"] < 600
    )
    if cache_ok:
        return _air_cache["data"]
    lat, lon = _owm_geocode(loc, key)
    url = "https://api.openweathermap.org/data/2.5/air_pollution"
    r = requests.get(url, params={"lat": lat, "lon": lon, "appid": key}, timeout=10)
    r.raise_for_status()
    js = r.json()
    first = (js.get("list") or [{}])[0]
    aqi = (first.get("main") or {}).get("aqi")
    comps = first.get("components") or {}
    labels = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}
    colors = {1: "#009966", 2: "#ffde33", 3: "#ff9933", 4: "#cc0033", 5: "#660099"}
    data = {"aqi": aqi, "label": labels.get(aqi, "?"), "color": colors.get(aqi, "#fff")}
    for k in ("pm2_5", "pm10", "no2", "o3", "so2", "co", "nh3"):
        v = comps.get(k)
        if v is not None:
            data[k] = v
    _air_cache.update({"key": key, "loc": loc, "ts": now, "data": data})
    return data

# === [SECTION: Bus (Seoul/Gyeonggi/Incheon) API adapters] ====================
def _as_int(x, default=None):
    try:
        return int(x)
    except Exception:
        return default

def _extract_min_from_msg(msg: str):
    """한국어 도착메시지에서 '분' 또는 '곧 도착' 판단."""
    if not msg:
        return None
    if "곧" in msg:
        return 0
    m = re.search(r"(\d+)\s*분", msg)
    return _as_int(m.group(1)) if m else None
def _pick_text(elem: Optional[ET.Element], tag: str) -> str:
    if elem is None:
        return ""
    child = elem.find(tag)
    return html.unescape(child.text) if (child is not None and child.text) else ""


def _normalize_arrmsg(msg: str, fallback_seconds: Optional[int]) -> Tuple[str, str]:
    """메시지에서 'N분', 'N번째 전' 등을 추출"""
    if not msg and fallback_seconds is None:
        return ("", "")
    if fallback_seconds is not None:
        if fallback_seconds < 120:
            return ("곧 도착", "1정거장")
        minutes = fallback_seconds // 60
        return (f"{minutes}분", "1정거장")
    m_min = re.search(r"(\d+)\s*분", msg or "")
    m_hops = re.search(r"(\d+)\s*번째\s*전", msg or "")
    if m_min and int(m_min.group(1)) < 2:
        t = "곧 도착"
    else:
        t = f"{m_min.group(1)}분" if m_min else ("곧 도착" if "곧 도착" in (msg or "") else (msg or ""))
    hops = f"{m_hops.group(1)}정거장" if m_hops else ""
    if not hops or hops == "0정거장":
        hops = "1정거장"
    return (t, hops)


def tago_get_arrivals(city_code: str, node_id: str, service_key: str) -> Tuple[str, List[str]]:
    url = (
        "http://apis.data.go.kr/1613000/BusArrivalService/getBusArrivalList"
        f"?serviceKey={quote(service_key)}&cityCode={quote(str(city_code))}&nodeId={quote(str(node_id))}"
    )
    r = requests.get(url, timeout=7)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    records: List[Tuple[int, str]] = []
    stop_name = ""
    for it in root.iter("item"):
        if not stop_name:
            stop_name = _pick_text(it, "nodenm") or _pick_text(it, "nodeNm")
        rtNm = _pick_text(it, "routeno") or _pick_text(it, "routeNo")
        arr = _pick_text(it, "arrtime") or _pick_text(it, "predictTime1")
        hops_raw = _pick_text(it, "arrsttnm") or _pick_text(it, "arriveRemainSeatCnt")
        seconds = int(arr) if arr and arr.isdigit() else None
        t1, hops = _normalize_arrmsg("", seconds)
        if hops_raw and hops_raw.isdigit():
            hops = f"{hops_raw}정거장"
        if not rtNm:
            continue
        line = "\t".join(filter(None, [rtNm, hops, t1]))
        m = re.search(r"(\d+)", t1)
        minutes = 0 if t1 == "곧 도착" else (int(m.group(1)) if m else 99999)
        records.append((minutes, line))
    records = [r for r in records if r[1].strip()]
    records.sort(key=lambda x: x[0])
    lines = [r[1] for r in records]
    return stop_name, lines


def fetch_bus():
    cfg = CFG.get("bus", {}) or {}
    city = cfg.get("city_code", "").strip()
    node = cfg.get("node_id", "").strip()
    key = cfg.get("key", "").strip()
    if not (city and node and key):
        return {"need_config": True}
    stop_name, lines = tago_get_arrivals(city, node, key)
    items = []
    for ln in lines:
        parts = ln.split("\t")
        rt = parts[0] if len(parts) > 0 else ""
        hops = parts[1] if len(parts) > 1 else ""
        tmsg = parts[2] if len(parts) > 2 else ""
        items.append({
            "route": rt,
            "msg1": tmsg,
            "msg2": hops,
            "min1": _extract_min_from_msg(tmsg),
        })
    items.sort(key=lambda x: x["min1"] if x["min1"] is not None else 9999)
    items = items[:14]
    return {"city_code": city, "stop_name": stop_name, "node_id": node, "items": items}


# === [SECTION: Standalone Bus Arrival Telegram Bot (PTB)] ====================
try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        ForceReply,
    )
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ModuleNotFoundError:  # pragma: no cover - 테스트 환경용 더미
    Update = InlineKeyboardButton = InlineKeyboardMarkup = ForceReply = object  # type: ignore
    Application = CallbackQueryHandler = CommandHandler = MessageHandler = object  # type: ignore

    class DummyContextTypes:
        """Fallback context holder when telegram.ext is unavailable."""

        DEFAULT_TYPE = object

    ContextTypes = DummyContextTypes  # type: ignore

    class filters:  # type: ignore
        TEXT = COMMAND = None

BB_BOT_TOKEN = CFG["telegram"]["bot_token"]
BB_ALLOWED_USER_IDS = set(CFG["telegram"].get("allowed_user_ids", []))
BB_TAGO_SERVICE_KEY = os.environ.get("TAGO_API_KEY", CFG.get("bus", {}).get("key", "")).strip()
BB_DEFAULT_CITY = CFG.get("bus", {}).get("city_code", "")
BB_DEFAULT_NODE = CFG.get("bus", {}).get("node_id", "")

BB_USER_STATE: Dict[int, Dict[str, Any]] = {}
BB_CITY_CACHE: List[Tuple[str, str]] = []
bb_log = logging.getLogger("busbot")


def bb_ensure_user_state(uid: int) -> Dict[str, Any]:
    if uid not in BB_USER_STATE:
        BB_USER_STATE[uid] = {
            "city_code": BB_DEFAULT_CITY,
            "node_id": BB_DEFAULT_NODE,
            "key": BB_TAGO_SERVICE_KEY,
            "awaiting": None,
            "stop_results": [],
        }
    return BB_USER_STATE[uid]


def bb_check_auth(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in BB_ALLOWED_USER_IDS)


def bb_extract_arg(text: str) -> str:
    parts = text.strip().split(maxsplit=2)
    if len(parts) == 2:
        return parts[1]
    if len(parts) >= 3:
        return parts[2]
    return ""


def bb_extract_arg2(text: str) -> List[str]:
    parts = text.strip().split()
    return parts[2:] if len(parts) >= 3 else []


def bb_is_tago_node_id(text: str) -> bool:
    """ICB164000104 형태의 TAGO nodeId인지 검사"""
    return bool(re.fullmatch(r"[A-Z]{3}\d{7,}", text.strip()))


def bb_paginate(items: List[Any], page: int, per_page: int = 10) -> List[Any]:
    start = page * per_page
    end = start + per_page
    return items[start:end]


def bb_tago_get_city_list(service_key: str) -> List[Tuple[str, str]]:
    """(cityName, cityCode) 리스트 반환"""
    if BB_CITY_CACHE:
        return BB_CITY_CACHE
    url = (
        "http://apis.data.go.kr/1613000/BusRouteInfoInqireService/getCtyCodeList"
        f"?serviceKey={quote(service_key)}"
    )
    try:
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception as e:
        log.warning("City list fetch failed: %s", e)
        return []
    for it in root.iter("item"):
        name = _pick_text(it, "cityname") or _pick_text(it, "cityName")
        code = _pick_text(it, "citycode") or _pick_text(it, "cityCode")
        if name and code:
            BB_CITY_CACHE.append((name, code))
    return BB_CITY_CACHE


def bb_tago_search_stops(city_code: str, keyword: str, service_key: str) -> List[Tuple[str, str, str]]:
    """(정류소명, 정류소번호, nodeId) 리스트 반환"""
    url = (
        "http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getSttnList"
        f"?serviceKey={quote(service_key)}&cityCode={quote(str(city_code))}&nodeNm={quote(keyword)}"
    )
    r = requests.get(url, timeout=7)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    stops: List[Tuple[str, str, str]] = []
    for it in root.iter("item"):
        name = _pick_text(it, "nodenm") or _pick_text(it, "nodeNm")
        ars = _pick_text(it, "arsno") or _pick_text(it, "arsNo")
        node = _pick_text(it, "nodeid") or _pick_text(it, "nodeId")
        if name and node:
            stops.append((name, ars, node))
    return stops


def bb_build_city_keyboard(cities: List[Tuple[str, str]], page: int = 0) -> InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(cities, page, per)
    buttons = [
        [InlineKeyboardButton(f"{name}", callback_data=f"city:{code}")]
        for name, code in items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"citypage:{page-1}"))
    if (page + 1) * per < len(cities):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"citypage:{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def bb_build_stop_keyboard(stops: List[Tuple[str, str, str]], page: int = 0) -> InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(stops, page, per)
    buttons = [
        [InlineKeyboardButton(f"{name} {ars}", callback_data=f"stop:{node}")]
        for name, ars, node in items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"stoppage:{page-1}"))
    if (page + 1) * per < len(stops):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"stoppage:{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


async def bb_cmd_bus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    st = bb_ensure_user_state(update.effective_user.id)
    city, node, key = st["city_code"], st["node_id"], st["key"]
    await update.message.reply_text(f"⏳ 조회 중… (city={city}, node={node})")
    stop_name, lines = tago_get_arrivals(city, node, key)
    await update.message.reply_text("\n".join(lines) if lines else "정보가 없습니다.")


async def bb_cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    st = bb_ensure_user_state(update.effective_user.id)
    text = (update.message.text or "").strip()
    if text.lower().startswith("/set key"):
        arg = bb_extract_arg(text)
        if not arg:
            await update.message.reply_text("사용법: /set key <서비스키>")
            return
        st["key"] = arg.strip()
        await update.message.reply_text("✔ 서비스키 등록 완료")
    else:
        await update.message.reply_text("사용법: /set key <서비스키>")


async def bb_cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    st = bb_ensure_user_state(update.effective_user.id)
    cities = bb_tago_get_city_list(st["key"])
    if not cities:
        st["awaiting"] = "keyword"
        await update.message.reply_text(
            "도시 목록을 가져오지 못했습니다. 서비스 키를 확인하거나 직접 도시 코드를 입력하세요."
        )
        return
    await update.message.reply_text(
        "도시를 선택하세요", reply_markup=bb_build_city_keyboard(cities, 0)
    )


async def bb_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = update.effective_user.id
    st = bb_ensure_user_state(uid)

    if data.startswith("citypage:"):
        page = int(data.split(":", 1)[1])
        cities = bb_tago_get_city_list(st["key"])
        await query.edit_message_reply_markup(bb_build_city_keyboard(cities, page))
        return
    if data.startswith("city:"):
        code = data.split(":", 1)[1]
        st["city_code"] = code
        st["awaiting"] = "keyword"
        await query.message.reply_text(
            "정류소명을 입력하세요", reply_markup=ForceReply(selective=True)
        )
        return
    if data.startswith("stoppage:"):
        page = int(data.split(":", 1)[1])
        stops = st.get("stop_results", [])
        await query.edit_message_reply_markup(bb_build_stop_keyboard(stops, page))
        return
    if data.startswith("stop:"):
        node = data.split(":", 1)[1]
        st["node_id"] = node
        st["awaiting"] = None
        await query.message.reply_text(f"✔ 정류소 등록 완료: {node}")
        return


async def bb_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    uid = update.effective_user.id
    st = bb_ensure_user_state(uid)
    if st.get("awaiting") == "keyword":
        kw = (update.message.text or "").strip()
        if not st.get("city_code"):
            if bb_is_tago_node_id(kw):
                st["node_id"] = kw
                st["awaiting"] = None
                await update.message.reply_text(f"✔ 정류소 등록 완료: {kw}")
                return
            if kw.isdigit():
                st["city_code"] = kw
                await update.message.reply_text(
                    "정류소명을 입력하세요", reply_markup=ForceReply(selective=True)
                )
                return
            await update.message.reply_text("도시 코드를 먼저 입력하세요.")
            return
        if bb_is_tago_node_id(kw):
            st["node_id"] = kw
            st["awaiting"] = None
            await update.message.reply_text(f"✔ 정류소 등록 완료: {kw}")
            return
        stops = bb_tago_search_stops(st["city_code"], kw, st["key"])
        st["stop_results"] = stops
        st["awaiting"] = "stop"
        await update.message.reply_text(
            "정류소를 선택하세요", reply_markup=bb_build_stop_keyboard(stops, 0)
        )


def run_bus_bot():
    app = Application.builder().token(BB_BOT_TOKEN).build()
    app.add_handler(CommandHandler("bus", bb_cmd_bus))
    app.add_handler(CommandHandler("set", bb_cmd_set))
    app.add_handler(CommandHandler("stop", bb_cmd_stop))
    app.add_handler(CallbackQueryHandler(bb_on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bb_on_message))
    bb_log.info("Bus bot started.")
    app.run_polling()


# Telebot helper keyboards for bus configuration
def tb_build_city_keyboard(cities: List[Tuple[str, str]], page: int = 0) -> telebot.types.InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(cities, page, per)
    kb = telebot.types.InlineKeyboardMarkup()
    for name, code in items:
        kb.add(telebot.types.InlineKeyboardButton(f"{name}", callback_data=f"bus_city:{code}"))
    nav = []
    if page > 0:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"bus_citypage:{page-1}"))
    if (page + 1) * per < len(cities):
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"bus_citypage:{page+1}"))
    if nav:
        kb.row(*nav)
    return kb


def tb_build_stop_keyboard(
    stops: List[Tuple[str, str, str]], page: int = 0
) -> telebot.types.InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(stops, page, per)
    kb = telebot.types.InlineKeyboardMarkup()
    for name, ars, node in items:
        label = f"{name} {ars}" if ars else name
        kb.add(telebot.types.InlineKeyboardButton(label, callback_data=f"bus_stop:{node}"))
    nav = []
    if page > 0:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"bus_stoppage:{page-1}"))
    if (page + 1) * per < len(stops):
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"bus_stoppage:{page+1}"))
    if nav:
        kb.row(*nav)
    return kb


# === [SECTION: Photo file listing for board background] ======================
def list_local_images():
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    files = []
    for p in sorted(PHOTOS_DIR.glob("**/*")):
        if p.is_file() and p.suffix.lower() in exts:
            files.append(str(p.relative_to(PHOTOS_DIR)))
    return files


def tb_build_photo_keyboard(page: int = 0, per: int = 20) -> telebot.types.InlineKeyboardMarkup:
    files = list_local_images()
    kb = telebot.types.InlineKeyboardMarkup()
    items = bb_paginate(files, page, per)
    for i, name in enumerate(items):
        idx = page * per + i
        kb.add(
            telebot.types.InlineKeyboardButton(name, callback_data=f"photo_sel:{page}:{idx}")
        )
    nav = []
    if page > 0:
        nav.append(telebot.types.InlineKeyboardButton("Prev", callback_data=f"photo_page:{page-1}"))
    if (page + 1) * per < len(files):
        nav.append(telebot.types.InlineKeyboardButton("Next", callback_data=f"photo_page:{page+1}"))
    if nav:
        kb.row(*nav)
    return kb


def tb_photo_confirm_keyboard(page: int, idx: int) -> telebot.types.InlineKeyboardMarkup:
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton("삭제", callback_data=f"photo_del_ok:{page}:{idx}"),
        telebot.types.InlineKeyboardButton("취소", callback_data=f"photo_del_no:{page}"),
    )
    return kb

# === [SECTION: Flask app / session / proxy headers] ==========================
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SFRAME_SESSION_SECRET", "CHANGE_ME_32CHARS")
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="None")

# === [SECTION: Verse helpers + API endpoints] ================================
def get_verse() -> str:
    # 위에서 선언한 공용 헬퍼 사용
    return _read_block(VER_START, VER_END).strip()

def set_verse(text: str):
    # 소스의 EMBEDDED_VERSES 블록을 즉시 갱신
    _write_block((text or "").strip(), VER_START, VER_END)
    # 선택: 텍스트 파일도 함께 갱신(원하셨던 verse txt 파일)
    try:
        (BASE / "verse.txt").write_text((text or "").strip() + "\n", encoding="utf-8")
    except Exception:
        pass

@app.get("/api/verse")
def api_verse():
    return jsonify({"text": get_verse()})

# === [SECTION: Todoist helpers + API endpoint] ===============================
def todoist_headers():
    tok = (CFG.get("todoist", {}) or {}).get("api_token", "").strip() or os.environ.get("SFRAME_TODOIST_TOKEN", "").strip()
    if not tok:
        # 토큰 없으면 need_config 표기
        raise RuntimeError("Todoist API token missing")
    return {"Authorization": f"Bearer {tok}"}

def todoist_list_tasks():
    """
    Fetch open tasks via REST v2.
    Respects filter/project_id; returns trimmed fields up to max_items (default 20).
    """
    base = "https://api.todoist.com/rest/v2/tasks"
    cfg = CFG.get("todoist", {}) or {}
    params = {}
    if cfg.get("project_id"):
        params["project_id"] = cfg["project_id"]
    if cfg.get("filter"):
        params["filter"] = cfg["filter"]
    # request
    r = requests.get(base, headers=todoist_headers(), params=params, timeout=10)
    r.raise_for_status()
    items = r.json()
    out = []
    max_items = int(cfg.get("max_items", 20))
    for t in items[:max_items]:
        out.append({
            "id": t.get("id"),
            "title": t.get("content"),
            "due": (t.get("due") or {}).get("date"),  # YYYY-MM-DD or RFC3339
            "priority": t.get("priority"),
            "project_id": t.get("project_id"),
            "url": t.get("url"),
        })
    return out

@app.get("/api/todo")
def api_todo():
    try:
        return jsonify(todoist_list_tasks())
    except Exception as e:
        return jsonify({"error": str(e), "need_config": True}), 200

# === [SECTION: Google OAuth helpers / Calendar service] ======================
def have_google_libs():
    return GOOGLE_OK

def load_google_creds():
    if not GTOKEN_PATH.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(GTOKEN_PATH), scopes=CFG["google"]["scopes"])
    except Exception:
        return None

def save_google_creds(creds: "Credentials"):
    GTOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

def get_google_service():
    if not have_google_libs():
        raise RuntimeError("Google libraries not installed. pip install google-auth google-auth-oauthlib google-api-python-client")
    creds = load_google_creds()
    if not creds:
        raise RuntimeError("No Google token. Visit /oauth/start to authorize.")
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request as GRequest
        creds.refresh(GRequest()); save_google_creds(creds)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

# === [SECTION: Google Calendar helpers (view/edit/delete)] ===================
def _cal_id():
    return CFG["google"]["calendar"].get("id", "primary")

def _fmt_start_end(ev):
    def pick(obj):
        if "dateTime" in obj:
            dt = datetime.fromisoformat(obj["dateTime"].replace("Z", "+00:00")).astimezone(TZ)
            return dt.strftime("%Y-%m-%d %H:%M")
        return obj.get("date", "")
    s = pick(ev["start"]); e = pick(ev["end"])
    return f"{s} ~ {e}"


# === [SECTION: REST API endpoints used by the board HTML] ====================
@app.get("/api/events")
def api_events():
    url = CFG["frame"]["ical_url"]
    if not url:
        return jsonify([])
    try:
        y = int(request.args.get("year")) if request.args.get("year") else None
        m = int(request.args.get("month")) if request.args.get("month") else None
    except Exception:
        y = m = None
    now_kst = datetime.now(TZ)
    y = y or now_kst.year
    m = m or now_kst.month
    items = month_filter(fetch_ical(url), y, m)
    return jsonify(items)

@app.get("/api/weather")
def api_weather():
    try:
        data = fetch_weather()
        return jsonify(data or {"need_config": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/air")
def api_air():
    try:
        data = fetch_air_quality()
        return jsonify(data or {"need_config": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/photos")
def api_photos():
    return jsonify(list_local_images())

@app.get("/photos/<path:fname>")
def serve_photo(fname):
    return send_from_directory(str(PHOTOS_DIR), fname)

@app.get("/api/bus")
def api_bus():
    try:
        data = fetch_bus()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/config")
def api_config():
    return jsonify({
        "frame": {"ical_url": CFG["frame"].get("ical_url", "")},
        "weather": {"api_key": CFG["weather"].get("api_key", "")},
        "todoist": {"api_token": CFG["todoist"].get("api_token", "")},
        "bus": {
            "key": CFG["bus"].get("key", ""),
            "city_code": CFG["bus"].get("city_code", ""),
            "node_id": CFG["bus"].get("node_id", ""),
        },
        "telegram": {"bot_token": CFG["telegram"].get("bot_token", "")},
    })

@app.post("/api/config/set")
def api_config_set():
    js = request.get_json(force=True, silent=True) or {}
    path = js.get("path", "")
    value = js.get("value", "")
    cur = CFG
    parts = path.split(".")
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            return jsonify({"error": "invalid path"}), 400
        cur = cur[p]
    cur[parts[-1]] = value
    if path == "frame.ical_url":
        global _ical_cache
        _ical_cache = {"url": None, "ts": 0.0, "events": []}
    return jsonify({"ok": True})

@app.post("/api/config/save")
def api_config_save():
    save_config_to_source(CFG)
    return jsonify({"ok": True})

@app.post("/api/upload-photo")
def api_upload_photo():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "no filename"}), 400
    ext = os.path.splitext(f.filename)[1] or ".jpg"
    fname = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
    f.save(PHOTOS_DIR / fname)
    return jsonify({"ok": True, "filename": fname})

@app.post("/api/delete-photos")
def api_delete_photos():
    js = request.get_json(force=True, silent=True) or {}
    files = js.get("files") or []
    removed = []
    for name in files:
        p = PHOTOS_DIR / name
        try:
            if p.exists():
                p.unlink()
                removed.append(name)
        except Exception:
            continue
    return jsonify({"ok": True, "removed": removed})


# === [SECTION: Board HTML (legacy UI; monthly calendar + photo fade)] ========
BOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
<title>Smart Frame</title>
<style>
  :root { --W:1080px; --H:1828px; --top:70px; --cal:910px;
          --bus:198px; --weather:280px; --todo:270px; } /* bus box reduced 25% */

  /* Global layout */
  html,body { margin:0; padding:0; background:transparent; color:#fff; font-family:system-ui,-apple-system,Roboto,'Noto Sans KR',sans-serif; }
  .frame { width:var(--W); height:var(--H); margin:0 auto; display:flex; flex-direction:column; position:relative; }

  /* Background photo crossfade */
  .bg, .bg2 {
    position: fixed; inset: 0; z-index: -1;
    background-size: cover; background-position: center center; background-repeat: no-repeat;
    transition: opacity 1s ease;
  }
  .bg2 { opacity: 0; }

  .top { height:var(--top); display:flex; align-items:center; justify-content:space-between; padding:0 24px; box-sizing:border-box; }
  .time { font-size:38px; font-weight:700; letter-spacing:1px; text-shadow:0 0 6px rgba(0,0,0,.65);}
  .date { font-size:22px; opacity:.95; text-shadow:0 0 6px rgba(0,0,0,.65);}
  .top-right{display:flex;align-items:center;gap:20px}
  .logout{font-size:22px;opacity:.95;color:#fff;text-decoration:none;text-shadow:0 0 6px rgba(0,0,0,.65)}

  .cal { height:var(--cal); padding:8px 20px; box-sizing:border-box; display:flex; flex-direction:column; }
  .cal h2 { margin:0 0 8px 0; font-size:22px; opacity:.95; display:flex; align-items:center; gap:8px; text-shadow:0 0 6px rgba(0,0,0,.65);}

  .grid { flex:1 1 auto; display:grid; grid-template-columns: repeat(7, 1fr); grid-auto-rows: 1fr; gap:6px; }
  .dow { display:grid; grid-template-columns: repeat(7, 1fr); margin-bottom:6px; opacity:.95; font-size:14px; text-shadow:0 0 6px rgba(0,0,0,.65);}
  .dow div { text-align:center; }

  /* Calendar cells */
  .cell { border:1px solid rgba(255,255,255,.12); border-radius:10px; padding:6px;
          background:rgba(0,0,0,.35); display:flex; flex-direction:column; overflow:hidden;}
  .cell.dim { opacity:.45; }
  .dnum { font-size:14px; opacity:.95; margin-bottom:4px; text-shadow:0 0 6px rgba(0,0,0,.65);}
  .ev { font-size:12px; line-height:1.25; margin:2px 0;
        background:rgba(0,0,0,.45); border-radius:6px; padding:2px 6px;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis; text-shadow:0 0 6px rgba(0,0,0,.65);}

  .section { height: calc(var(--H) - var(--top) - var(--cal)); padding:10px 24px; box-sizing:border-box; display:flex; flex-direction:column; gap:10px; }

  .blk { background:rgba(0,0,0,.35); border:1px solid rgba(255,255,255,.08); border-radius:12px; padding:10px 12px; }
  .blk h3 { margin:0 0 6px 0; font-size:16px; opacity:.95; text-shadow:0 0 6px rgba(0,0,0,.65);}

.todo{ flex:0 0 var(--todo); display:flex; flex-direction:column;}
  .todo .rows { display:grid; grid-template-columns: 1fr 1fr; gap:8px; }
  .todo .col { display:flex; flex-direction:column; gap:6px; min-width:0; }
  .todo .item { display:flex; justify-content:flex-start; gap:10px; font-size:14px; }
  .todo .title { flex:1 1 auto; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .todo .due { opacity:.9; min-width:50px; margin-right:12px; }

  .bus{flex:0 0 var(--bus); display:flex; flex-direction:column;}
  .bus .stop{font-size:14px; margin-bottom:4px;}
  .bus .rows{display:flex; gap:10px; overflow:hidden;}
  .bus .col{flex:1 1 50%; display:flex; flex-direction:column; gap:6px;}
  .bus .item{display:flex; font-size:14px; white-space:nowrap;}
  .bus .item .rt{font-weight:700; width:8ch; white-space:nowrap;}
  .bus .item .hops{width:6ch; text-align:right; margin-right:4px; white-space:nowrap;}
  .bus .item .msg{flex:0 0 6ch; text-align:right; opacity:.9; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}

  /* Verse block */
  .verse { flex:0 0 100px; display:flex; flex-direction:column; align-items:flex-start; }
  .verse .text { white-space:pre-wrap; line-height:1.4; font-size:16px; text-shadow:0 0 6px rgba(0,0,0,.65); }

/* Weather layout (card style 5-day forecast) */
.weather {
  display:flex;
  gap:16px;
  align-items:stretch;
}
.weather .w-now {
  display:flex;
  align-items:center;
  gap:12px;
  min-width:180px;
}
.weather .w-now .temp { font-size:44px; font-weight:800; line-height:1; }

.weather .w-days {
  display:grid;
  grid-template-columns:repeat(5,1fr);
  gap:12px;
  width:100%;
  align-items:stretch;
  flex:1 1 auto;
}
.weather .w-day {
  text-align:center;
  background:rgba(0,0,0,.25);
  border:1px solid rgba(255,255,255,.08);
  border-radius:12px;
  padding:10px 6px;
  min-width:0;
}
.weather .w-day.today { outline:2px solid rgba(255,255,255,.35); outline-offset:-2px; }
.weather .w-day img { width:72px; height:72px; display:block; margin:6px auto; }
.weather .w-day .temps { display:flex; justify-content:center; gap:8px; font-size:14px; margin-top:4px; }
.weather .w-day .hi { font-weight:800; font-size:16px; }
.weather .w-day .lo { opacity:.75; font-size:14px; }

/* ▼ AQI card on the right */
.weather .w-aqi {
  width:140px;
  text-align:center;
  background:rgba(0,0,0,.25);
  border:1px solid rgba(255,255,255,.08);
  border-radius:12px;
  padding:12px 8px;
  display:flex;
  flex-direction:column;
  justify-content:center;
  gap:4px;
  margin-left:auto;
}
.weather .w-aqi .ttl { font-size:12px; letter-spacing:.5px; opacity:.9; }
.weather .w-aqi .idx { font-size:24px; font-weight:800; line-height:1; }
.weather .w-aqi .lbl { font-size:14px; opacity:.9; }
.weather .w-aqi .pm { font-size:12px; opacity:.85; }

/* Background must stay behind content */
.bg, .bg2 { z-index:-1; }
.frame { position:relative; z-index:1; }

</style>
</head>
<body>
<div class="bg" id="bg1"></div>
<div class="bg2" id="bg2"></div>

<div class="frame">
  <div class="top">
    <div class="time" id="clock">--:--</div>
    <div class="top-right">
      <div class="date" id="datetxt">----</div>
      <a href="/logout" class="logout">Logout</a>
    </div>
  </div>

  <div class="cal">
    <h2 id="cal-title">Calendar</h2>
    <div class="dow"><div>Sun</div><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div></div>
    <div class="grid" id="grid"></div>
  </div>

  <div class="section">
    <div class="verse blk"><h3>Today's Verse</h3><div id="verse" class="text"></div></div>
    <div class="todo blk">
      <h3>Todo</h3>
      <div class="rows">
        <div class="col" id="todo-col-1"></div>
        <div class="col" id="todo-col-2"></div>
      </div>
    </div>
    <div class="bus blk">
      <h3 id="bus-title">BUS Info</h3>
      <div class="stop" id="bus-stop"></div>
      <div class="rows" id="businfo">
        <div class="col" id="bus-left"></div>
        <div class="col" id="bus-right"></div>
      </div>
    </div>
    <div class="weather blk" id="weather"></div>
  </div>
</div>

<script>
function z(n){return n<10?'0'+n:n}
function tick(){
  const d=new Date();
  const days=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  document.getElementById('clock').textContent = z(d.getHours())+":"+z(d.getMinutes());
  document.getElementById('datetxt').textContent = d.getFullYear()+"."+z(d.getMonth()+1)+"."+z(d.getDate())+" ("+days[d.getDay()]+")";
}
setInterval(tick, 1000); tick();

function startOfWeek(d){ const day=d.getDay(); const s=new Date(d); s.setDate(d.getDate()-day); s.setHours(0,0,0,0); return s; }

async function loadEvents(){
  const d=new Date();
  const y=d.getFullYear(), m=d.getMonth()+1;
  document.getElementById('cal-title').textContent = `Calendar  ${y}-${z(m)}`;
  const r = await fetch(`/api/events?year=${y}&month=${m}`);
  const items = await r.json();

  const byDay = {};
  for(const ev of items){
    const k = (ev.start||'').substring(0,10);
    (byDay[k]=byDay[k]||[]).push(ev);
  }

  const first = new Date(y, m-1, 1);
  let cur = startOfWeek(first);
  const grid = document.getElementById('grid'); grid.innerHTML='';
  let count = 0;
  while(count < 42){
    const cell = document.createElement('div');
    cell.className = 'cell' + ((cur.getMonth()+1!==m)?' dim':'');
    const key = `${cur.getFullYear()}-${z(cur.getMonth()+1)}-${z(cur.getDate())}`;
    const dn  = document.createElement('div'); dn.className='dnum'; dn.textContent = cur.getDate();
    cell.appendChild(dn);
    const arr = (byDay[key]||[]).slice(0,3);
    for(const ev of arr){
      const e=document.createElement('div'); e.className='ev'; e.textContent = ev.title || '(untitled)';
      cell.appendChild(e);
    }
    grid.appendChild(cell);
    cur.setDate(cur.getDate()+1);
    count++;
  }
}
loadEvents(); setInterval(loadEvents, 5*60*1000);




// ===== Weather block (final: card-style 5-day forecast) =====
async function loadWeather() {
  const box = document.getElementById('weather');
  try {
    // 날씨 + AQI 동시 요청
    const [wr, ar] = await Promise.all([
      fetch('/api/weather'),
      fetch('/api/air')
    ]);

    const data = await wr.json();
    const air  = await ar.json().catch(()=>null);

    box.innerHTML = '';

    if (data && data.need_config) { box.textContent = 'OWM API Key required'; return; }
    if (!data || data.error)     { box.textContent = 'Weather error';       return; }

    // 현재(좌측)
    const now = document.createElement('div');
    now.className = 'w-now';
    const i = document.createElement('img');
    i.src = data.current.icon; i.alt = ''; i.style.width='70px'; i.style.height='70px';
    const t = document.createElement('div');
    t.className = 'temp';
    t.textContent = data.current.temp + '°';
    now.appendChild(i); now.appendChild(t);

    // 5일 카드(중앙) — 데이터가 7일 와도 5개만 사용
    const days = document.createElement('div');
    days.className = 'w-days';
    const names = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const todayIso = new Date().toISOString().slice(0,10);

    const fiveDays = (Array.isArray(data.days) ? data.days : []).slice(0, 5);
    for (const d of fiveDays) {
      const dt = new Date(d.date);
      const item = document.createElement('div');
      item.className = 'w-day';
      if (d.date === todayIso) item.classList.add('today');

      const nm = document.createElement('div'); nm.className='nm'; nm.textContent = names[dt.getDay()];
      const im = document.createElement('img'); im.src = d.icon; im.alt = '';
      const temps = document.createElement('div'); temps.className='temps';
      const hi = document.createElement('div'); hi.className='hi'; hi.textContent = d.max + '°';
      const lo = document.createElement('div'); lo.className='lo'; lo.textContent = d.min + '°';
      temps.appendChild(hi); temps.appendChild(lo);

      item.appendChild(nm); item.appendChild(im); item.appendChild(temps);
      days.appendChild(item);
    }

    // AQI 카드(우측 끝)
    const aqiCard = document.createElement('div');
    aqiCard.className = 'w-aqi';
    const ttl = document.createElement('div'); ttl.className='ttl'; ttl.textContent = 'AQI';
    const idx = document.createElement('div'); idx.className='idx';
    const lbl = document.createElement('div'); lbl.className='lbl';
    const pm25 = document.createElement('div'); pm25.className='pm pm25';
    const pm10 = document.createElement('div'); pm10.className='pm pm10';

    if (air && !air.error && !air.need_config) {
      idx.textContent = air.aqi != null ? String(air.aqi) : '?';
      lbl.textContent = air.label || '';
      if (air.color) {
        aqiCard.style.boxShadow = `inset 0 0 0 2px ${air.color}`;
        aqiCard.style.color = '#fff';
      }
      if (air.pm2_5 != null) pm25.textContent = 'PM2.5 ' + Math.round(air.pm2_5);
      if (air.pm10  != null) pm10.textContent  = 'PM10 '  + Math.round(air.pm10);
    } else {
      idx.textContent = '–';
      lbl.textContent = 'n/a';
    }
    aqiCard.appendChild(ttl); aqiCard.appendChild(idx); aqiCard.appendChild(lbl);
    if (pm25.textContent) aqiCard.appendChild(pm25);
    if (pm10.textContent) aqiCard.appendChild(pm10);

    // 조립
    box.appendChild(now);
    box.appendChild(days);
    box.appendChild(aqiCard);

  } catch (e) {
    if (box) box.textContent = 'Failed to load weather';
  }
}
loadWeather();
setInterval(loadWeather, 10 * 60 * 1000);

// ===== Verse block =====
async function loadVerse(){
  try{
    const r = await fetch('/api/verse');
    const js = await r.json();
    document.getElementById('verse').textContent = js.text || '';
  }catch(e){
    document.getElementById('verse').textContent = '';
  }
}
loadVerse(); setInterval(loadVerse, 10*1000);

// ===== Todo block (Todoist, 2 columns, 10 each) =====
function fmtDue(v){
  if(!v) return '';
  const d = new Date(v);
  if (isNaN(d.getTime())) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    if (m) return m[2] + '/' + m[3];
    return v;
  }
  return (d.getMonth()+1) + '/' + d.getDate();
}

async function loadTodo(){
  try{
    const r = await fetch('/api/todo');
    const data = await r.json();
    const c1 = document.getElementById('todo-col-1');
    const c2 = document.getElementById('todo-col-2');
    c1.innerHTML = ''; c2.innerHTML = '';

    if (data.need_config){
      const msg = document.createElement('div'); msg.textContent = 'Todoist API token required';
      c1.appendChild(msg);
      return;
    }
    if (!Array.isArray(data) || data.length === 0){
      const msg = document.createElement('div'); msg.textContent = 'No pending tasks.';
      c1.appendChild(msg);
      return;
    }

    const first10 = data.slice(0,10);
    const next10  = data.slice(10,20);

    for (const t of first10){
      const row = document.createElement('div'); row.className='item';
      const date = document.createElement('div'); date.className='due'; date.textContent = fmtDue(t.due) || '';
      const title = document.createElement('div'); title.className='title'; title.textContent = t.title || '(untitled)';
      row.appendChild(date); row.appendChild(title); c1.appendChild(row);
    }
    for (const t of next10){
      const row = document.createElement('div'); row.className='item';
      const date = document.createElement('div'); date.className='due'; date.textContent = fmtDue(t.due) || '';
      const title = document.createElement('div'); title.className='title'; title.textContent = t.title || '(untitled)';
      row.appendChild(date); row.appendChild(title); c2.appendChild(row);
    }
  }catch(e){
    // ignore
  }
}
loadTodo(); setInterval(loadTodo, 20*1000);

async function refreshBus(){
  try{
    const r = await fetch('/api/bus');
    if(!r.ok) return;
    const data = await r.json();
    const stopEl = document.getElementById('bus-stop');
    if(stopEl) stopEl.textContent = data.stop_name ? `${data.stop_name} (${data.node_id || ''})` : '';

    const left = document.getElementById('bus-left');
    const right = document.getElementById('bus-right');
    if(!left || !right) return;

    left.innerHTML='';
    right.innerHTML='';
    if(data.need_config){
      left.textContent = '버스 설정 필요';
      return;
    }
    const items = data.items || [];
    const mid = Math.ceil(items.length/2);
    items.slice(0, mid).forEach(it=>{
      const row=document.createElement('div');
      row.className='item';
      row.innerHTML=`<div class="rt">${it.route}</div><div class="hops">${it.msg2}</div><div class="msg">${it.msg1}</div>`;
      left.appendChild(row);
    });
    items.slice(mid).forEach(it=>{
      const row=document.createElement('div');
      row.className='item';
      row.innerHTML=`<div class="rt">${it.route}</div><div class="hops">${it.msg2}</div><div class="msg">${it.msg1}</div>`;
      right.appendChild(row);
    });
  }catch(e){}
}
refreshBus();
setInterval(refreshBus,60000);

// ===== Background photo crossfade (delay-optimized & path-safe) =====
// - /api/photos 목록 셔플
// - 세그먼트별 URL 인코딩(하위 폴더 유지)
// - Image().decode()로 미리 디코드 후 전환
// - 초기 한 장은 화면에 바로 세팅하고 큐에서 소비 → 첫 전환 즉시 다른 사진
// - 탭 비활성화 시 타이머 일시중지

let photoList = [];
let pi = 0;           // 사진 인덱스
let front = 1;        // 현재 보이는 레이어: 1=bg1, 2=bg2

const DISPLAY_INTERVAL_MS = 5000;
const PRELOAD_MIN_COUNT   = 2;
const PRELOAD_COOLDOWN_MS = 250;

let preloadQueue = [];     // [{ url, readyAt }]
let isPreloading = false;
let nextSwitchAt = 0;
let slideTimer = null;
let refillTimer = null;

// --- 유틸: 세그먼트별 인코딩(하위 폴더 유지) -------------------------------
function buildPhotoUrl(name){
  // "a/b c.jpg" -> "/photos/a/b%20c.jpg"
  return '/photos/' + String(name).split('/').map(encodeURIComponent).join('/');
}

// --- 유틸: 배열 셔플 -------------------------------------------------------
function shuffle(arr){
  for (let i = arr.length - 1; i > 0; i--){
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
}

// --- 목록 로드 --------------------------------------------------------------
async function loadPhotos(){
  try{
    const r = await fetch('/api/photos');
    photoList = await r.json();
    shuffle(photoList);
  }catch(e){
    console.error('[photos] load failed:', e);
    photoList = [];
  }
}

// --- 이미지 1장 프리로드(+decode) -----------------------------------------
function preloadOne(url){
  return new Promise((resolve)=>{
    const img = new Image();
    let done = false;
    const finish = ok => { if (!done){ done = true; resolve(ok ? img : null); } };
    img.onload = ()=>{
      if (img.decode){
        img.decode().then(()=>finish(true)).catch(()=>finish(true));
      }else{
        finish(true);
      }
    };
    img.onerror = ()=> finish(null);
    img.src = url;
  });
}

// --- 프리로드 큐 보충 ------------------------------------------------------
async function ensurePreloaded(){
  if (isPreloading) return;
  isPreloading = true;
  try{
    while (preloadQueue.length < PRELOAD_MIN_COUNT && photoList.length){
      const name = photoList[pi % photoList.length]; pi++;
      const url  = buildPhotoUrl(name);
      const ok   = await preloadOne(url);
      if (ok){
        preloadQueue.push({ url, readyAt: Date.now() + PRELOAD_COOLDOWN_MS });
      }
    }
  }finally{
    isPreloading = false;
  }
}

// --- 실제 전환 --------------------------------------------------------------
function swapBackground(nextUrl){
  const incoming = document.getElementById(front === 1 ? 'bg2' : 'bg1'); // 들어올 레이어(현재 투명)
  incoming.style.backgroundImage = `url("${nextUrl}")`;
  // reflow
  incoming.offsetHeight;
  incoming.style.opacity = 1;

  const outgoing = document.getElementById(front === 1 ? 'bg1' : 'bg2'); // 나갈 레이어(현재 보임)
  outgoing.style.opacity = 0;

  front = 3 - front;
}

// --- 한 스텝 전환 ----------------------------------------------------------
async function showNextPhoto(){
  if (!photoList.length) return;

  await ensurePreloaded();
  if (!preloadQueue.length) return;

  const now = Date.now();
  if (now < nextSwitchAt) return;

  const { url, readyAt } = preloadQueue[0];
  if (now < readyAt) return;

  preloadQueue.shift();
  swapBackground(url);
  nextSwitchAt = now + DISPLAY_INTERVAL_MS;

  // 백그라운드 프리로드
  ensurePreloaded();
}

// --- 타이머 컨트롤/가시성 대응 --------------------------------------------
function stopPhotoTimers(){
  if (slideTimer){ clearInterval(slideTimer); slideTimer = null; }
  if (refillTimer){ clearInterval(refillTimer); refillTimer = null; }
}
function startPhotoTimers(){
  if (!slideTimer){
    slideTimer = setInterval(showNextPhoto, DISPLAY_INTERVAL_MS);
  }
  if (!refillTimer){
    refillTimer = setInterval(async ()=>{
      if (!photoList.length){
        await loadPhotos();
      }
      ensurePreloaded();
    }, 60 * 1000);
  }
}
document.addEventListener('visibilitychange', ()=>{
  if (document.hidden){
    stopPhotoTimers();
  }else{
    nextSwitchAt = Date.now();
    startPhotoTimers();
  }
});

// --- 초기화(IIFE) -----------------------------------------------------------
(async ()=>{
  stopPhotoTimers();

  await loadPhotos();
  if (!photoList.length){
    return;
  }

  await ensurePreloaded();

  const b1 = document.getElementById('bg1');
  const b2 = document.getElementById('bg2');

  // 초기 1장 화면 세팅(큐에서 소비)
  if (preloadQueue[0]){
    const first = preloadQueue.shift();
    b1.style.backgroundImage = `url("${first.url}")`;
    b1.style.opacity = 1;
    b2.style.opacity = 0;
    front = 1;
    nextSwitchAt = Date.now() + DISPLAY_INTERVAL_MS;
  }

  // 다음 전환용으로 숨김 레이어 미리 세팅(있다면)
  if (preloadQueue[0]){
    b2.style.backgroundImage = `url("${preloadQueue[0].url}")`;
  }

  startPhotoTimers();
  showNextPhoto(); // 준비됐으면 바로 1회 시도
})();
</script>
</body>
</html>
"""
SETTING_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Settings</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Noto Sans KR',sans-serif;padding:20px;line-height:1.6}
table{border-collapse:collapse}
td{padding:4px}
#photo-gallery{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
#photo-gallery .thumb{position:relative}
#photo-gallery img{width:100px;height:100px;object-fit:cover}
#photo-gallery input{position:absolute;top:2px;left:2px}
.logout{position:fixed;top:10px;right:10px}
</style>
</head>
<body>
<a href="/logout" class="logout">Logout</a>
<h2>Settings</h2>
<div id="config"></div>
<h3>Photos</h3>
<input type="file" id="photo-file" accept="image/*" multiple style="display:none">
<button id="upload-btn">Upload</button>
<button id="delete-btn">Delete</button>
<button id="toggle-btn">Show Photos</button>
<div id="photo-gallery" style="display:none;"></div>
<script>
const fields=[
  {label:'iCal URL',path:'frame.ical_url'},
  {label:'Weather API key',path:'weather.api_key'},
  {label:'Todoist API token',path:'todoist.api_token'},
  {label:'Bus API key',path:'bus.key'},
  {label:'Bus city code',path:'bus.city_code'},
  {label:'Bus stop ID',path:'bus.node_id'},
  {label:'Telegram bot token',path:'telegram.bot_token'}
];
let pendingFiles=[];
function getValue(obj,path){return path.split('.').reduce((o,k)=>o&&o[k]!=null?o[k]:'',obj);}
async function loadConfig(){
  const r=await fetch('/api/config');const cfg=await r.json();
  const cont=document.getElementById('config');const tbl=document.createElement('table');
  fields.forEach(f=>{
    const tr=document.createElement('tr');
    const td1=document.createElement('td');td1.textContent=f.label;tr.appendChild(td1);
    const td2=document.createElement('td');
    const inp=document.createElement('input');
    inp.value=getValue(cfg,f.path);
    inp.dataset.real=inp.value;
    inp.dataset.hidden='1';
    inp.type='text';
    inp.size=8;
    inp.value='********';
    td2.appendChild(inp);
    const eye=document.createElement('span');
    const eyeOpen='<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';
    const eyeClosed='<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
    eye.innerHTML=eyeClosed;
    eye.style.cursor='pointer';eye.style.marginLeft='4px';
    eye.onclick=()=>{
      if(inp.dataset.hidden==='1'){
        inp.dataset.hidden='0';
        inp.type='text';
        inp.size=40;
        inp.value=inp.dataset.real;
        eye.innerHTML=eyeOpen;
      }else{
        inp.dataset.real=inp.value;
        inp.dataset.hidden='1';
        inp.type='text';
        inp.size=8;
        inp.value='********';
        eye.innerHTML=eyeClosed;
      }
    };
    td2.appendChild(eye);tr.appendChild(td2);
    const td3=document.createElement('td');
    const btn=document.createElement('button');btn.textContent='확인';
    btn.onclick=async()=>{
      const val=inp.dataset.hidden==='1'?inp.dataset.real:inp.value;
      await fetch('/api/config/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:f.path,value:val})});
    };
    td3.appendChild(btn);tr.appendChild(td3);
    tbl.appendChild(tr);f.input=inp;
  });
  cont.appendChild(tbl);
  const saveBtn=document.createElement('button');saveBtn.textContent='저장';
  saveBtn.onclick=async()=>{
    for(const file of pendingFiles){
      const fd=new FormData();fd.append('file',file);
      await fetch('/api/upload-photo',{method:'POST',body:fd});
    }
    pendingFiles=[];
    await fetch('/api/config/save',{method:'POST'});
    loadPhotos();
    alert('Saved');
  };
  cont.appendChild(saveBtn);
}
async function loadPhotos(){
  const g=document.getElementById('photo-gallery');g.innerHTML='';
  const r=await fetch('/api/photos');const arr=await r.json();
  arr.forEach(name=>{
    const div=document.createElement('div');div.className='thumb';
    const chk=document.createElement('input');chk.type='checkbox';chk.value=name;div.appendChild(chk);
    const img=document.createElement('img');img.src='/photos/'+encodeURIComponent(name);div.appendChild(img);
    g.appendChild(div);
  });
}
const fi=document.getElementById('photo-file');
const toggleBtn=document.getElementById('toggle-btn');
document.getElementById('upload-btn').onclick=()=>fi.click();
fi.onchange=()=>{
  if(!fi.files.length)return;
  const g=document.getElementById('photo-gallery');
  for(const file of fi.files){
    pendingFiles.push(file);
    const div=document.createElement('div');div.className='thumb pending';
    const img=document.createElement('img');img.src=URL.createObjectURL(file);
    div.appendChild(img);
    g.appendChild(div);
  }
  fi.value='';
  if(g.style.display==='none'){g.style.display='flex';toggleBtn.textContent='Hide Photos';}
};
document.getElementById('delete-btn').onclick=async()=>{
  const sel=[...document.querySelectorAll('#photo-gallery input:checked')].map(ch=>ch.value);
  if(!sel.length)return;
  await fetch('/api/delete-photos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:sel})});
  loadPhotos();
};
toggleBtn.onclick=()=>{
  const g=document.getElementById('photo-gallery');
  const hide=g.style.display==='none';
  g.style.display=hide?'flex':'none';
  toggleBtn.textContent=hide?'Hide Photos':'Show Photos';
};
loadConfig();loadPhotos();
</script>
</body>
</html>
"""

LOGIN_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Login</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Noto Sans KR',sans-serif;padding:20px}
form{max-width:300px;margin:auto;display:flex;flex-direction:column;gap:6px}
input{padding:8px}
a{font-size:0.9em}
</style>
</head>
<body>
<form method="post">
  <input name="user" placeholder="Username" required>
  <input name="pw" type="password" placeholder="Password" required>
  <button type="submit">Login</button>
  <div><a href="#">Forgot your password?</a> | <a href="#">Create an account</a></div>
</form>
</body>
</html>
"""

LANDING_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Home</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Noto Sans KR',sans-serif;padding:20px}
.card{width:220px;height:150px;border:1px solid #ccc;position:relative;display:flex;align-items:center;justify-content:center}
.menu{position:absolute;top:5px;right:5px}
.menu button{background:none;border:none;font-size:20px;cursor:pointer}
.popup{display:none;position:absolute;top:25px;right:5px;border:1px solid #ccc;background:#fff}
.popup a{display:block;padding:5px 10px;text-decoration:none;color:#000}
</style>
</head>
<body>
<a href="/logout">Logout</a>
<div class="card">
  <div class="menu"><button id="menu-btn">⋯</button>
    <div id="menu-popup" class="popup">
      <a href="/board">View</a>
      <a href="/setting">Setting</a>
    </div>
  </div>
  <div>My Screen</div>
</div>
<script>
const btn=document.getElementById('menu-btn');
const pop=document.getElementById('menu-popup');
btn.onclick=()=>{pop.style.display=pop.style.display==='block'?'none':'block';};
document.addEventListener('click',e=>{if(e.target!==btn && !pop.contains(e.target))pop.style.display='none';});
</script>
</body>
</html>
"""

def require_login(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if request.cookies.get("auth") != "1":
            return "<p>로그인이 필요합니다. <a href='/login'>Login</a></p>", 401
        return fn(*a, **k)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        user = request.form.get("user", "")
        pw = request.form.get("pw", "")
        if user == "root" and pw == "qwer1234":
            resp = make_response(redirect("/home"))
            resp.set_cookie("auth", "1", max_age=86400, httponly=True)
            return resp
        return render_template_string(LOGIN_HTML), 401
    return render_template_string(LOGIN_HTML)

@app.get("/logout")
def logout():
    resp = make_response(redirect("/login"))
    resp.delete_cookie("auth")
    return resp

@app.get("/home")
@require_login
def home_after_login():
    return render_template_string(LANDING_HTML)

@app.get("/board")
@require_login
def board():
    return render_template_string(BOARD_HTML)

@app.get("/setting")
@require_login
def setting_page():
    return render_template_string(SETTING_HTML)

# === [SECTION: Bot state helpers (persist to json file)] ===

def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(d):
    STATE_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

# === [SECTION: Telegram bot initialization / ACL] ============================
TB = telebot.TeleBot(CFG["telegram"]["bot_token"]) if CFG["telegram"]["bot_token"] else None
ALLOWED = set(CFG["telegram"]["allowed_user_ids"])
def allowed(uid): return uid in ALLOWED if ALLOWED else True

def kb_inline(rows):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    for r in rows: kb.add(*r)
    return kb

# === [SECTION: Telegram handlers (commands, callbacks, text)] ================
if TB:
    @TB.message_handler(commands=["set"])
    def set_cmd(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "Not authorized.")
        kb = kb_inline([
            [telebot.types.InlineKeyboardButton("1) calendar iCal URL", callback_data="cfg_ical")],
            [telebot.types.InlineKeyboardButton("2) 버스정보", callback_data="cfg_bus")],
            [telebot.types.InlineKeyboardButton("3) weather api", callback_data="cfg_weather")],
            [telebot.types.InlineKeyboardButton("4) verse", callback_data="set_verse")],
            [telebot.types.InlineKeyboardButton("5) todo api", callback_data="cfg_todo")],
            [telebot.types.InlineKeyboardButton("6) photo", callback_data="cfg_photo")],
        ])
        TB.send_message(m.chat.id, "Select category:", reply_markup=kb)

    @TB.callback_query_handler(func=lambda c: c.data in ("cfg_ical", "cfg_weather", "set_verse", "cfg_bus", "cfg_todo", "cfg_photo"))
    def on_cb(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        if c.data == "cfg_ical":
            url = CFG["frame"].get("ical_url", "(not set)")
            st = load_state(); st[str(c.from_user.id)] = {"wait": "ical"}; save_state(st)
            TB.answer_callback_query(c.id)
            TB.send_message(c.message.chat.id, f"Current iCal URL:\n{url}\n\nSend a new URL, or /cancel to abort.")
        elif c.data == "cfg_weather":
            TB.answer_callback_query(c.id)
            key = CFG.get("weather", {}).get("api_key", "(not set)")
            st = load_state(); st[str(c.from_user.id)] = {"mode": "await_weather_api"}; save_state(st)
            TB.send_message(c.message.chat.id, f"Current OpenWeather API key:\n{key}\n\nSend a new key, or /cancel to abort.")
        elif c.data == "set_verse":
            TB.answer_callback_query(c.id)
            st = load_state(); st[str(c.from_user.id)] = {"mode": "await_verse"}; save_state(st)
            TB.send_message(c.message.chat.id, "input text")
        elif c.data == "cfg_todo":
            TB.answer_callback_query(c.id)
            key = CFG.get("todoist", {}).get("api_token", "(not set)")
            st = load_state(); st[str(c.from_user.id)] = {"mode": "await_todo_api"}; save_state(st)
            TB.send_message(c.message.chat.id, f"Current Todoist API token:\n{key}\n\nSend a new token, or /cancel to abort.")
        elif c.data == "cfg_photo":
            TB.answer_callback_query(c.id)
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("삭제", callback_data="photo_delete"),
                telebot.types.InlineKeyboardButton("추가", callback_data="photo_add"),
            )
            TB.send_message(c.message.chat.id, "사진 관리:", reply_markup=kb)
        elif c.data == "cfg_bus":
            TB.answer_callback_query(c.id)
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("정류소 변경", callback_data="bus_set_stop"),
                telebot.types.InlineKeyboardButton("서비스키 변경", callback_data="bus_set_key"),
                telebot.types.InlineKeyboardButton("현재설정 조회", callback_data="bus_show_config"),
                telebot.types.InlineKeyboardButton("지정정류소 현황조회", callback_data="bus_test"),
            )
            TB.send_message(c.message.chat.id, "버스 정보 메뉴를 선택하세요:", reply_markup=kb)

    @TB.callback_query_handler(func=lambda c: c.data.startswith("photo_"))
    def on_cb_photo(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        uid = c.from_user.id
        data = c.data
        if data == "photo_add":
            st = load_state(); st[str(uid)] = {"mode": "await_photo"}; save_state(st)
            TB.send_message(c.message.chat.id, "사진을 올려주세요.")
        elif data == "photo_delete":
            files = list_local_images()
            if not files:
                TB.send_message(c.message.chat.id, "사진이 없습니다.")
            else:
                TB.send_message(
                    c.message.chat.id,
                    "삭제할 사진을 선택하세요:",
                    reply_markup=tb_build_photo_keyboard(0),
                )
        elif data.startswith("photo_page:"):
            page = int(data.split(":", 1)[1])
            TB.edit_message_reply_markup(
                c.message.chat.id,
                c.message.message_id,
                reply_markup=tb_build_photo_keyboard(page),
            )
        elif data.startswith("photo_sel:"):
            _, page, idx = data.split(":")
            page = int(page); idx = int(idx)
            files = list_local_images()
            if 0 <= idx < len(files):
                name = files[idx]
                TB.send_message(
                    c.message.chat.id,
                    f"{name} 삭제할까요?",
                    reply_markup=tb_photo_confirm_keyboard(page, idx),
                )
        elif data.startswith("photo_del_ok:"):
            _, page, idx = data.split(":")
            page = int(page); idx = int(idx)
            files = list_local_images()
            if 0 <= idx < len(files):
                name = files[idx]
                try:
                    (PHOTOS_DIR / name).unlink()
                    TB.send_message(c.message.chat.id, f"삭제 완료: {name}")
                except Exception as e:
                    TB.send_message(c.message.chat.id, f"삭제 실패: {e}")
            files = list_local_images()
            if files:
                max_page = max((len(files) - 1) // 20, 0)
                page = min(page, max_page)
                TB.send_message(
                    c.message.chat.id,
                    "삭제할 사진을 선택하세요:",
                    reply_markup=tb_build_photo_keyboard(page),
                )
            else:
                TB.send_message(c.message.chat.id, "사진이 없습니다.")
        elif data.startswith("photo_del_no:"):
            TB.edit_message_reply_markup(c.message.chat.id, c.message.message_id)
    # ---- Bus settings flow
    @TB.callback_query_handler(func=lambda c: c.data in ("bus_set_stop", "bus_set_key", "bus_show_config", "bus_test"))
    def on_cb_bus(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        uid = c.from_user.id
        if c.data == "bus_set_stop":
            key = CFG.get("bus", {}).get("key", "").strip()
            if not key:
                TB.send_message(c.message.chat.id, "서비스키가 설정되지 않았습니다.")
                return
            cities = bb_tago_get_city_list(key)
            st = load_state(); st[str(uid)] = {"mode": "bus_city"}; save_state(st)
            TB.send_message(
                c.message.chat.id,
                "도시를 선택하세요",
                reply_markup=tb_build_city_keyboard(cities, 0),
            )
        elif c.data == "bus_set_key":
            st = load_state(); st[str(uid)] = {"mode": "await_bus_key"}; save_state(st)
            TB.send_message(c.message.chat.id, "서비스키를 입력하세요.")
        elif c.data == "bus_show_config":
            bus = CFG.get("bus", {})
            city = bus.get("city_code", "설정안됨")
            node = bus.get("node_id", "설정안됨")
            key_status = "등록" if bus.get("key") else "미등록"
            msg = [
                f"도시코드: {city}",
                f"노드ID: {node}",
                f"서비스키: {key_status}",
            ]
            TB.send_message(c.message.chat.id, "\n".join(msg))
        elif c.data == "bus_test":
            try:
                data = fetch_bus()
                if data.get("need_config"):
                    TB.send_message(c.message.chat.id, "버스 설정이 부족합니다.")
                    return
                lines = [data.get("stop_name", "")]
                for it in data.get("items", [])[:10]:
                    parts = [it["route"]]
                    if it.get("msg2"):
                        parts.append(it["msg2"])
                    if it.get("msg1"):
                        parts.append(it["msg1"])
                    lines.append("\t".join(parts))
                if len(lines) == 1:
                    lines.append("(정보 없음)")
                TB.send_message(c.message.chat.id, "\n".join(lines))
            except Exception as e:
                TB.send_message(c.message.chat.id, f"검색 실패: {e}")

    @TB.callback_query_handler(
        func=lambda c: c.data.startswith(
            ("bus_city", "bus_citypage", "bus_stop", "bus_stoppage")
        )
    )
    def on_cb_bus_flow(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        uid = c.from_user.id
        st_all = load_state()
        st = st_all.get(str(uid), {})
        if c.data.startswith("bus_citypage:"):
            page = int(c.data.split(":", 1)[1])
            key = CFG.get("bus", {}).get("key", "")
            cities = bb_tago_get_city_list(key)
            TB.edit_message_reply_markup(
                c.message.chat.id,
                c.message.message_id,
                reply_markup=tb_build_city_keyboard(cities, page),
            )
            return
        if c.data.startswith("bus_city:"):
            code = c.data.split(":", 1)[1]
            st_all[str(uid)] = {"mode": "bus_keyword", "city_code": code}
            save_state(st_all)
            TB.send_message(
                c.message.chat.id,
                "정류소명을 입력하세요",
                reply_markup=telebot.types.ForceReply(selective=False),
            )
            return
        if c.data.startswith("bus_stoppage:"):
            page = int(c.data.split(":", 1)[1])
            stops = st.get("stop_results", [])
            TB.edit_message_reply_markup(
                c.message.chat.id,
                c.message.message_id,
                reply_markup=tb_build_stop_keyboard(stops, page),
            )
            return
        if c.data.startswith("bus_stop:"):
            node = c.data.split(":", 1)[1]
            CFG["bus"]["city_code"] = st.get("city_code", "")
            CFG["bus"]["node_id"] = node
            save_config_to_source(CFG)
            st_all.pop(str(uid), None); save_state(st_all)
            TB.send_message(c.message.chat.id, f"정류소 등록 완료: {node}")
            return

    @TB.message_handler(content_types=["photo"])
    def on_photo(m):
        st = load_state().get(str(m.from_user.id))
        if not st or st.get("mode") != "await_photo":
            return
        try:
            file_id = m.photo[-1].file_id
            info = TB.get_file(file_id)
            data = TB.download_file(info.file_path)
            ext = os.path.splitext(info.file_path)[1] or ".jpg"
            fname = f"{int(time.time())}_{file_id}{ext}"
            (PHOTOS_DIR / fname).write_bytes(data)
            TB.reply_to(m, f"업로드 완료: {fname}")
        except Exception as e:
            TB.reply_to(m, f"업로드 실패: {e}")

    @TB.message_handler(commands=["cancel"])
    def cancel(m):
        st = load_state(); st.pop(str(m.from_user.id), None); save_state(st)
        TB.reply_to(m, "Canceled.")

    @TB.message_handler(func=lambda m: True)
    def on_text(m):
        st = load_state().get(str(m.from_user.id))
        if not st: return

        # update iCal url
        if st.get("wait") == "ical":
            new_url = m.text.strip()
            if not (new_url.startswith("http://") or new_url.startswith("https://")):
                TB.reply_to(m, "Invalid URL. Please send http/https URL."); return
            CFG["frame"]["ical_url"] = new_url
            save_config_to_source(CFG)
            global _ical_cache
            _ical_cache = {"url": None, "ts": 0.0, "events": []}
            TB.reply_to(m, "iCal URL updated. Board will auto-refresh in ~1-2 min.")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # set verse text
        if st.get("mode") == "await_verse":
            txt = (m.text or "").strip()
            set_verse(txt)
            TB.reply_to(m, "Verse updated.")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # weather api key
        if st.get("mode") == "await_weather_api":
            val = (m.text or "").strip()
            CFG["weather"]["api_key"] = val
            save_config_to_source(CFG)
            TB.reply_to(m, "Weather API key updated.")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # todo api token
        if st.get("mode") == "await_todo_api":
            val = (m.text or "").strip()
            CFG["todoist"]["api_token"] = val
            save_config_to_source(CFG)
            TB.reply_to(m, "Todo API token updated.")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # bus: keyword -> search stops or accept nodeId directly
        if st.get("mode") in ("bus_keyword", "bus_stop"):
            kw = (m.text or "").strip()
            city = st.get("city_code", "")
            if bb_is_tago_node_id(kw):
                CFG["bus"]["city_code"] = city
                CFG["bus"]["node_id"] = kw
                save_config_to_source(CFG)
                TB.reply_to(m, "변경완료")
                allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
                return
            key = CFG.get("bus", {}).get("key", "")
            stops = bb_tago_search_stops(city, kw, key)
            if not stops:
                TB.reply_to(m, "검색 결과가 없습니다. 다시 입력해주세요.")
                return
            st.update({"mode": "bus_stop", "stop_results": stops})
            save_state({**load_state(), str(m.from_user.id): st})
            TB.send_message(
                m.chat.id,
                "정류소를 선택하세요",
                reply_markup=tb_build_stop_keyboard(stops, 0),
            )
            return

        # bus: api key
        if st.get("mode") == "await_bus_key":
            val = (m.text or "").strip()
            CFG["bus"]["key"] = val
            save_config_to_source(CFG)
            TB.reply_to(m, "변경완료")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

# === [SECTION: Telegram start (webhook or polling) + duplication guard] ======
# - 파일락(/tmp/scal_bot.lock)으로 중복 폴링 방지 (다중 토큰/다중 인스턴스 보호)
_lock_file = None
def start_telegram():
    """Start telegram in single-instance mode using a file lock."""
    global _lock_file
    if not TB:
        print("[TG] Telegram not configured (no bot token).")
        return
    # acquire lock file to avoid double polling
    try:
        _lock_file = open("/tmp/scal_bot.lock", "w")
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
    except Exception:
        print("[TG] Another instance is already running. Skipping telegram start.")
        return

    mode = CFG["telegram"].get("mode", "polling")
    if mode == "webhook":
        base = CFG["telegram"].get("webhook_base", "").rstrip("/")
        if not base:
            print("[TG] webhook mode, but webhook_base missing; fallback to polling")
            return start_polling()
        secret = CFG["telegram"].get("path_secret") or secrets.token_urlsafe(24)
        CFG["telegram"]["path_secret"] = secret
        save_config_to_source(CFG)
        hook_url = f"{base}/tg/{secret}"
        TB.remove_webhook()
        TB.set_webhook(url=hook_url, drop_pending_updates=True)
        print(f"[TG] Telegram webhook set: {hook_url}")

        @app.post(f"/tg/{secret}")
        def tg_webhook():
            if request.headers.get("content-type") != "application/json":
                abort(403)
            update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
            TB.process_new_updates([update])
            return "OK"
    else:
        start_polling()

def start_polling():
    TB.remove_webhook()
    print("[TG] Telegram polling started")
    TB.infinity_polling(timeout=60, long_polling_timeout=60, allowed_updates=["message", "callback_query"])

# === [SECTION: Google OAuth routes (home/start/callback/test)] ===============
HOME_HTML = r"""
<!doctype html><meta charset="utf-8">
<title>SCAL Home</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Noto Sans KR',sans-serif;padding:24px;line-height:1.6}</style>
<h2>SCAL  Smart Calendar</h2>
<a href="/logout">Logout</a>
<ul>
  <li><a href="/board" target="_blank">Open Board (/board)</a></li>
  <li><a href="/oauth/start">Start Google OAuth</a>  Calendar features</li>
</ul>
<hr>
<p>Status:
  <b>Google libs</b> : {{ 'OK' if google_ok else 'install required' }}<br>
  <b>Google token</b> : {{ 'connected' if token_ok else 'not connected' }}</p>
<p>Files:
  <code>{{ base }}/google_client_secret.json</code> (manual),
  <code>{{ base }}/google_token.json</code> (auto)
</p>
"""

@app.get("/")
@require_login
def home():
    return redirect("/home")

@app.get("/info")
@require_login
def info_page():
    return render_template_string(
        HOME_HTML,
        google_ok=have_google_libs(),
        token_ok=GTOKEN_PATH.exists(),
        base=str(BASE),
    )

@app.get("/oauth/start")
def oauth_start():
    if not have_google_libs():
        return "google-* libs missing. pip install google-auth google-auth-oauthlib google-api-python-client", 500
    if not GCLIENT_PATH.exists():
        return f"Client secret file missing: {GCLIENT_PATH}", 500
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"
    flow = Flow.from_client_secrets_file(str(GCLIENT_PATH), scopes=CFG["google"]["scopes"], redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    resp = make_response(redirect(auth_url))
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="None", secure=True)
    return resp

@app.get("/oauth/callback")
def oauth_callback():
    if not have_google_libs():
        return "google-* libs missing.", 500
    if not GCLIENT_PATH.exists():
        return f"Client secret file missing: {GCLIENT_PATH}", 500
    state_cookie = request.cookies.get("oauth_state")
    state_param = request.args.get("state")
    if not state_cookie or state_cookie != state_param:
        return "OAuth state mismatch.", 400
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"
    flow = Flow.from_client_secrets_file(str(GCLIENT_PATH), scopes=CFG["google"]["scopes"], redirect_uri=redirect_uri)
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        save_google_creds(creds)
    except Exception as e:
        return f"Token exchange failed: {e}", 400
    return redirect(url_for("home"))

@app.post("/oauth/test-insert")
def oauth_test_insert():
    try:
        svc = get_google_service()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    js = request.get_json(force=True, silent=True) or {}
    summary = js.get("summary") or "Test Event"
    start = js.get("start") or datetime.now(TZ).date().isoformat()
    end   = js.get("end") or start
    body = {"summary": summary, "start": {"date": start}, "end": {"date": end}}
    try:
        cal_id = CFG["google"]["calendar"]["id"]
        ev = svc.events().insert(calendarId=cal_id, body=body).execute()
        return jsonify({"ok": True, "id": ev.get("id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# === [SECTION: App entrypoints (web thread + telegram)] ======================
def run_web():
    # debug=False, use_reloader=False to prevent reloader double-start
    try:
        app.run(host="0.0.0.0", port=int(CFG["server"]["port"]), debug=False, use_reloader=False)
    except OSError:
        print("Address already in use")
        raise

def main():
    t = threading.Thread(target=run_web, daemon=True); t.start()
    print(f"[WEB] started on :{CFG['server']['port']}  -> /board")
    start_telegram()

if __name__ == "__main__":
    main()
