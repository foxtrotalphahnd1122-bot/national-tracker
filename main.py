import os
os.environ["TZ"] = "Asia/Tokyo"
import time
import json
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# === セッション設定 ===
session = requests.Session()
session.headers.update({"User-Agent": "NationalFleetUltimateTracker/20.0"})

# === Renderなどの常時起動プラットフォーム用ダミーサーバー ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        return

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

# === 設定項目 ===
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
# 更新間隔（デフォルトを早めの 300秒 = 5分 に設定。環境変数で変更可能）
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))

# 日本周辺の緯度経度範囲（日本エリア判定用）
JAPAN_LAT_MIN, JAPAN_LAT_MAX = 24.0, 46.0
JAPAN_LON_MIN, JAPAN_LON_MAX = 122.0, 146.0

# ナショナル・エアラインズ 全保有・リース機体の登録番号（レジ）
TARGET_REGISTRATIONS = {
    "N756CK", "N916CA", "N919CA", "N927CA", "N936CA", 
    "N949CA", "N952CA", "N953CA", "N954CA", # B747 貨物機群
    "N828CA", "N898CA", "N880CA",             # A330
    "N963CA",                                # B757
    "N792CA"                                 # B777F
}

HISTORY_FILE = "flight_history.json"

if not DISCORD_WEBHOOK_URL:
    raise ValueError("エラー: DISCORD_WEBHOOK_URL が設定されていません。")

aircraft_states = {}

def get_jst_now_str():
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime('%Y-%m-%d %H:%M:%S (JST)')

def log_info(message):
    print(f"🟢 [INFO] [{get_jst_now_str()}] {message}")

def log_error(message):
    print(f"❌ [ERROR] [{get_jst_now_str()}] {message}")

def load_flight_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_flight_history(history_data):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=4)
    except:
        pass

def record_flight_event(tail, flight, ac_type, status_type, lat, lon):
    history = load_flight_history()
    if tail not in history:
        history[tail] = {"total_sightings": 0, "logs": []}
    
    history[tail]["total_sightings"] += 1
    history[tail]["last_seen"] = get_jst_now_str()
    history[tail]["last_status"] = status_type
    
    event_entry = {
        "date": get_jst_now_str(),
        "flight": flight,
        "type": ac_type,
        "status": status_type,
        "lat": lat,
        "lon": lon
    }
    history[tail]["logs"].insert(0, event_entry)
    history[tail]["logs"] = history[tail]["logs"][:20]
    save_flight_history(history)
    return history[tail]["total_sightings"]

def get_direction_text(track):
    if track is None or not isinstance(track, (int, float)):
        return "不明"
    directions = ["北 (N)", "北北東 (NNE)", "北東 (NE)", "東北東 (ENE)", "東 (E)", "東南東 (ESE)", "南東 (SE)", "南南東 (SSE)", "南 (S)", "南南西 (SSW)", "南西 (SW)", "西南西 (WSW)", "西 (W)", "西北西 (WNW)", "北西 (NW)", "北北西 (NNW)"]
    idx = int((track + 11.25) / 22.5) % 16
    return f"{directions[idx]} ({int(track)}°)"

def get_location_info(lat, lon, icao):
    if lat is None or lon is None:
        return "位置情報なし", None, "#"
    
    coord_str = f"({round(lat, 4)}, {round(lon, 4)})"
    google_maps_url = f"https://www.google.com/maps?q={lat},{lon}"
    adsb_lol_map_url = f"https://globe.adsb.lol/?icao={icao}" if icao else "https://globe.adsb.lol/"

    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
        res = session.get(url, timeout=3).json()
        address = res.get("address", {})
        country = address.get("country", "")
        state = address.get("state", "")
        city = address.get("city") or address.get("town") or address.get("village", "")
        aerodrome = address.get("aerodrome") or address.get("military") or address.get("aeroway", "")

        parts = []
        if aerodrome: parts.append(f"施設/空港: {aerodrome}")
        if country: parts.append(f"国: {country}")
        if state: parts.append(f"地域: {state}")
        if city: parts.append(f"市区町村: {city}")

        if parts:
            desc = " / ".join(parts)
            loc_str = f"📍 **{desc}**\n　└ [Googleマップ]({google_maps_url}) | [🌐 ADS-B.lol レーダーで見る]({adsb_lol_map_url}) {coord_str}"
        else:
            loc_str = f"📍 [Googleマップ]({google_maps_url}) | [🌐 ADS-B.lol レーダーで見る]({adsb_lol_map_url}) {coord_str}"
        return loc_str, google_maps_url, adsb_lol_map_url
    except:
        loc_str = f"📍 [Googleマップ]({google_maps_url}) | [🌐 ADS-B.lol レーダーで見る]({adsb_lol_map_url}) {coord_str}"
        return loc_str, google_maps_url, adsb_lol_map_url

def is_near_japan(lat, lon):
    if lat is None or lon is None:
        return False
    return (JAPAN_LAT_MIN <= lat <= JAPAN_LAT_MAX) and (JAPAN_LON_MIN <= lon <= JAPAN_LON_MAX)

def send_notification(tail, flight, ac_type, alt, track, speed, lat, lon, status_type, sighting_count, icao):
    tail_str = tail if tail else "不明"
    flight_str = flight if flight else "不明"
    type_str = ac_type if ac_type else "B747 貨物機"
    
    alt_str = "地上 (Ground)" if alt == "ground" or (isinstance(alt, (int, float)) and alt < 100) else f"{alt:,} ft" if isinstance(alt, (int, float)) else str(alt)
    speed_str = f"{speed} kts (約 {int(speed * 1.852)} km/h)" if isinstance(speed, (int, float)) else "不明"
    direction_str = get_direction_text(track)
    
    location_str, _, adsb_url = get_location_info(lat, lon, icao)
    near_japan = is_near_japan(lat, lon)
    is_n936ca = ("936CA" in tail_str.upper())

    if near_japan:
        embed_color = 0xFF0000
        content_text = f"🇯🇵🚨 **【日本来航確定】ナショナル ({tail_str}) が日本のエリアに到達！（ステータス: {status_type}）** @everyone"
        title_prefix = f"🇯🇵 【日本エリア到達】({status_type})"
    elif is_n936ca:
        embed_color = 0xF1C40F
        content_text = f"⭐✈️ **【注目機 N936CA 検知】重要機体が動いています。（ステータス: {status_type}）**"
        title_prefix = f"⭐ 【N936CA】{status_type}"
    else:
        embed_color = 0x3498DB
        content_text = f"✈️ **【ナショナル運行情報】{tail_str} ({flight_str}) が {status_type} しました。**"
        title_prefix = f"✈️ 運行ステータス ({status_type})"

    embed_data = {
        "title": title_prefix,
        "color": embed_color,
        "fields": [
            {"name": "🕒 検知時刻 (JST)", "value": get_jst_now_str(), "inline": False},
            {"name": "📍 位置情報・レーダーリンク", "value": location_str, "inline": False},
            {"name": "機体番号 (Tail)", "value": f"🌟 **{tail_str}**" if is_n936ca else tail_str, "inline": True},
            {"name": "機種", "value": type_str, "inline": True},
            {"name": "コールサイン", "value": flight_str, "inline": True},
            {"name": "高度 / 速度", "value": f"{alt_str} / {speed_str}", "inline": True},
            {"name": "進行方位", "value": direction_str, "inline": True},
            {"name": "通算観測", "value": f"📊 **{sighting_count} 回目**", "inline": True},
            {"name": "日本エリア", "value": "🔴 圏内" if near_japan else "⚪ 海外", "inline": True},
        ],
        "footer": {"text": "National Global Ultimate Tracker"}
    }

    try:
        session.post(DISCORD_WEBHOOK_URL, json={"content": content_text, "embeds": [embed_data]}, timeout=5)
        log_info(f"通知送信成功: Tail={tail_str}")
    except Exception as e:
        log_error(f"Discord通知エラー: {e}")

def monitor_loop():
    global aircraft_states
    
    # 世界全体のデータを広範囲（25000nm）で一括取得
    bulk_url = "https://api.adsb.lol/v2/lat/0/lon/0/dist/25000"
    
    found_national = {}
    try:
        res = session.get(bulk_url, timeout=15)
        ac_list = res.json().get("ac", [])
        log_info(f"グローバルスキャン実施: 取得機体総数 = {len(ac_list)}機")
        
        for ac in ac_list:
            tail = ac.get("r", "").strip().upper()
            flight = ac.get("flight", "").strip().upper()
            ac_desc = ac.get("desc", "").strip().upper()
            ac_type = ac.get("t", "").strip().upper()
            
            # マッチング条件：
            # 1. 登録番号（レジ）がリストに完全一致
            # 2. コールサインが "NCR" で始まる
            # 3. 機種がB744等で、かつコールサインや演算子に関連ワードがある場合も含める
            is_match = False
            if tail in TARGET_REGISTRATIONS:
                is_match = True
            elif flight.startswith("NCR"):
                is_match = True
            elif "NATIONAL" in ac.get("ownOp", "").upper():
                is_match = True

            if is_match:
                icao = ac.get("hex", "").strip()
                if not tail and icao:
                    tail = icao
                found_national[tail] = ac

    except Exception as e:
        log_error(f"一括データ取得エラー: {e}")
        return

    # 30分に一回（またはループごと）、現在の検知機体数をログ＆Discordステータスレポートとして報告
    log_info(f"🎯 【ステータス報告】現在捕捉中のナショナル機数: {len(found_national)}機 (監視レジ対象: {len(TARGET_REGISTRATIONS)}機)")

    for tail, ac in found_national.items():
        flight = ac.get("flight", "N/A").strip()
        ac_type = ac.get("t", ac.get("desc", "不明")).strip()
        alt = ac.get("alt_baro")
        track = ac.get("track")
        speed = ac.get("speed")
        lat = ac.get("lat")
        lon = ac.get("lon")
        icao = ac.get("hex", "")

        if lat is None or lon is None:
            continue

        is_ground = (alt == "ground") or (isinstance(alt, (int, float)) and alt < 100)
        is_currently_in_air = not is_ground
        near_japan = is_near_japan(lat, lon)

        if tail not in aircraft_states:
            status_type = "地上駐機" if is_ground else "新規空中検知"
            sighting_count = record_flight_event(tail, flight, ac_type, status_type, lat, lon)
            aircraft_states[tail] = {
                "in_air": is_currently_in_air,
                "last_status": status_type,
                "near_japan": near_japan
            }
            send_notification(tail, flight, ac_type, alt, track, speed, lat, lon, status_type, sighting_count, icao)
        else:
            last_state = aircraft_states[tail]
            last_in_air = last_state["in_air"]
            last_near_japan = last_state["near_japan"]

            if last_in_air != is_currently_in_air:
                status_type = "離陸 (Takeoff)" if is_currently_in_air else "着陸 (Landing)"
                sighting_count = record_flight_event(tail, flight, ac_type, status_type, lat, lon)
                send_notification(tail, flight, ac_type, alt, track, speed, lat, lon, status_type, sighting_count, icao)
                aircraft_states[tail]["in_air"] = is_currently_in_air
                aircraft_states[tail]["last_status"] = status_type

            if not last_near_japan and near_japan:
                status_type = "日本エリア突入"
                sighting_count = record_flight_event(tail, flight, ac_type, status_type, lat, lon)
                send_notification(tail, flight, ac_type, alt, track, speed, lat, lon, status_type, sighting_count, icao)
                aircraft_states[tail]["near_japan"] = near_japan

if __name__ == "__main__":
    log_info("ナショナル・エアラインズ 究極トラッカーを開始します...")
    
    # 起動時テスト通知
    startup_payload = {
        "content": "✨ **【システム起動】ナショナル・エアラインズ 究極トラッカー（B747・ADS-Bリンク対応版）が稼働を開始しました！**",
        "embeds": [{
            "title": "🚀 監視システム稼働中",
            "color": 0x2ECC71,
            "fields": [
                {"name": "監視間隔", "value": f"{CHECK_INTERVAL}秒 ごと", "inline": True},
                {"name": "対象レジ数", "value": f"{len(TARGET_REGISTRATIONS)} 機 (B747貨物等含む)", "inline": True}
            ],
            "footer": {"text": "National Fleet Ultimate Tracker"}
        }]
    }
    try:
        session.post(DISCORD_WEBHOOK_URL, json=startup_payload, timeout=5)
    except:
        pass

    while True:
        try:
            monitor_loop()
        except Exception as e:
            log_error(f"メインループエラー: {e}")
        time.sleep(CHECK_INTERVAL)
