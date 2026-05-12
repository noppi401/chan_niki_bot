import os
import httpx
import sqlite3
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from hashlib import sha256
import json
from datetime import datetime, timedelta, date
from slowapi import Limiter

from fishing_mcp.locations import get_location, all_location_names, LOCATIONS
from fishing_mcp.lunar import lunar_age, tide_type, tide_type_rating, moon_phase_name
from fishing_mcp.tide import fetch_tide_info
from fishing_mcp.temperature import fetch_sea_temperature
from fishing_mcp.weather import fetch_weather
from slowapi.util import get_remote_address

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")

app = FastAPI()
limiter = Limiter(key_func=get_remote_address, default_limits=["80/day"])
app.state.limiter = limiter

DB_PATH = "cache.db"
CACHE_TTL_MINUTES = 60

# DB初期化
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            qhash TEXT PRIMARY KEY,
            query TEXT,
            response TEXT,
            timestamp DATETIME
        )""")
init_db()

# キャッシュ取得・保存
def get_cache(q: str):
    qhash = sha256(q.encode()).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT response, timestamp FROM cache WHERE qhash = ?", (qhash,))
        row = cursor.fetchone()
        if row:
            ts = datetime.fromisoformat(row[1])
            if datetime.now() - ts < timedelta(minutes=CACHE_TTL_MINUTES):
                return json.loads(row[0])
    return None

def save_cache(q: str, data: dict):
    qhash = sha256(q.encode()).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "REPLACE INTO cache (qhash, query, response, timestamp) VALUES (?, ?, ?, ?)",
            (qhash, q, json.dumps(data), datetime.now().isoformat())
        )

@app.get("/search")
@limiter.limit("10/minute")
async def search(request: Request, q: str = Query(...)):
    print(f"[MCP] 検索リクエスト受信: {q}")
    cached = get_cache(q)
    if cached:
        print("[MCP] キャッシュ命中")
        return {"query": q, "results": cached, "cached": True}

    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": q,
            "num": 5
        }
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        results = [
            f"{item['title']}: {item['snippet']}" for item in data.get("items", [])
        ]

        save_cache(q, results)
        return {"query": q, "results": results, "cached": False}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 釣り情報エンドポイント ──────────────────────────────────────────────

@app.get("/fishing/spots")
async def fishing_spots():
    """対応釣り場一覧を返す"""
    spots = []
    for loc in LOCATIONS.values():
        spots.append({
            "name": loc["name"],
            "location": f"{loc['prefecture']}{loc['city']}",
            "target_fish": loc["target_fish"],
            "description": loc["description"],
        })
    return {"spots": spots}


@app.get("/fishing/conditions")
async def fishing_conditions(
    location: str = Query(..., description="釣り場名"),
    target_date: str = Query("", description="YYYY-MM-DD形式。省略時は今日"),
):
    """釣り場の総合条件（潮汐・海水温・釣り適性）をテキストで返す"""
    loc = get_location(location)
    if loc is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"釣り場 '{location}' が見つかりません", "available": all_location_names()},
        )

    d: date
    if target_date:
        try:
            d = date.fromisoformat(target_date)
        except ValueError:
            d = date.today()
    else:
        d = date.today()

    age = lunar_age(datetime.combine(d, datetime.min.time()))
    ttype = tide_type(age)
    moon = moon_phase_name(age)
    rating, comment = tide_type_rating(ttype)

    lines = [
        f"【{loc['name']}】釣り条件 {d.strftime('%Y/%m/%d')}",
        f"潮の種類: {ttype}（月齢{age:.1f}日/{moon}）",
        f"釣り適性: {rating} — {comment}",
    ]

    try:
        tide = await fetch_tide_info(loc["tide_station_id"], d)
        if tide["high_tides"]:
            ht = " / ".join(f"{h['time']}({h['height_cm']}cm)" for h in tide["high_tides"])
            lines.append(f"満潮: {ht}")
        if tide["low_tides"]:
            lt = " / ".join(f"{h['time']}({h['height_cm']}cm)" for h in tide["low_tides"])
            lines.append(f"干潮: {lt}")
        if tide.get("tidal_range_cm"):
            lines.append(f"潮差: {tide['tidal_range_cm']}cm（{loc['tide_station_name']}基準）")
    except Exception as e:
        lines.append(f"※潮汐取得エラー: {e}")

    try:
        temp = await fetch_sea_temperature(loc["lat"], loc["lon"])
        if temp:
            lines.append(f"海水温: {temp['temperature_c']:.1f}°C（{temp['source']}）")
    except Exception:
        pass

    try:
        weather = await fetch_weather(
            loc.get("jma_area_code", "140010"),
            loc.get("jma_temp_code", "46106"),
            d,
        )
        if weather:
            lines.append("─")
            if weather.get("weather"):
                lines.append(f"天気: {weather['weather']}")
            if weather.get("wind"):
                lines.append(f"風: {weather['wind']}")
            if weather.get("waves"):
                lines.append(f"波: {weather['waves']}")
            if weather.get("pops"):
                lines.append(f"降水確率: {' / '.join(weather['pops'])}%")
            if weather.get("temp_min") and weather.get("temp_max"):
                lines.append(f"気温: 最低{weather['temp_min']}℃ / 最高{weather['temp_max']}℃")
            elif weather.get("temp_max"):
                lines.append(f"気温: 最高{weather['temp_max']}℃")
            if weather.get("fishing_weather_comment"):
                lines.append(f"釣り影響: {weather['fishing_weather_comment']}")
    except Exception as e:
        lines.append(f"※天気取得エラー: {e}")

    lines.append(f"狙える魚: {' / '.join(loc['target_fish'])}")
    lines.append(f"釣り方: {' / '.join(loc['fishing_style'])}")
    lines.append(loc["notes"])

    return {"location": loc["name"], "date": d.isoformat(), "summary": "\n".join(lines)}
