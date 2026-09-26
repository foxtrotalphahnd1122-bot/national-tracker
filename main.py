import os
os.environ["TZ"] = "Asia/Tokyo"
import time
import json
import math
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# === ネットワークセッションの持続化 ===
session = requests.Session()

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
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))

# 日本周辺の緯度経度範囲（日本エリア判定用）
JAPAN_LAT_MIN, JAPAN_LAT_MAX = 24.0, 46.0
JAPAN_LON_MIN, JAPAN_LON_MAX = 122.0, 146.0

ALLOWED_OPERATORS = ["national air", "national airlines", "ncr"]
TARGET_CALLSIGNS = ["national", "ncr"]

# 過去データを保存するファイル名
HISTORY_FILE = "flight_history.json"

if not DISCORD_WEBHOOK_URL:
    raise ValueError("エラー: DISCORD_WEBHOOK_URL が設定されていません。")

# 状態管理メモリ
aircraft_states = {}

def get_jst_now_str():
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime('%Y-%m-%d %H:%M:%S (JST)')

def log_info(message):
    print(f"🟢 [INFO] [{get_jst_now_str()}] {message}")

def log_warn(message):
    print(f"⚠️ [WARN] [{get_jst_now_str()}] {message}")

def log_error(message):
    print(f"❌ [ERROR] [{get_jst_now_str()}] {message}")

# === 過去データの読み書き管理関数 ===
def load_flight_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log_error(f"履歴ファイルの読み込みエラー: {e}")
    return {}

def save_flight_history(history_data):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        log_error(f"履歴ファイルの保存エラー: {e}")

def record_flight_event(tail, flight, ac_type, status_type, lat, lon):
    history = load_flight_history()
    today_str = datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')
    
    if tail not in history:
        history[tail] = {"total_sightings": 0, "logs": []}
    
    history[tail]["total_sightings"] += 1
    history[tail]["last_seen"] = get_jst_now_str()
    history[tail]["last_status"] = status_type
    
    # 最新のイベントを記録（最大20件まで保持）
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

def send_startup_notification():
    history = load_flight_history()
    recorded_count = len(history)
    payload = {
        "content": "✨ **【システム起動】ナショナル・エアラインズ 履歴学習型スマート・トラッカーが稼働を開始しました！**",
        "embeds": [{
            "title": "🚀 過去データ蓄積型フライト監視システム稼働中",
            "color": 0x2ECC71,
            "fields": [
                {"name": "監視対象", "value": "ナショナル・エアラインズ全機 ＆ N936CA", "inline": True},
                {"name": "蓄積済み機体データ", "value": f"{recorded_count} 機分の履歴をロード中", "inline": True},
                {"name": "チェック間隔", "value": f"{CHECK_INTERVAL}秒", "inline": True},
            ],
            "footer": {"text": "National Memory-Enabled Tracker"}
        }]
    }
    try:
        session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        log_info("起動通知の送信に成功しました。")
    except Exception as e:
        log_error(f"起動通知送信エラー: {e}")

def get_direction_text(track):
    if track is None or not isinstance(track, (int, float)):
        return "不明"
    directions = [
        "北 (N)", "北北東 (NNE)", "北東 (NE)", "東北東 (ENE)",
        "東 (E)", "東南東 (ESE)", "南東 (SE)", "南南東 (SSE)",
        "南 (S)", "南南西 (SSW)", "南西 (SW)", "西南西 (WSW)",
        "西 (W)", "西北西 (WNW)", "北西 (NW)", "北北西 (NNW)"
    ]
    idx = int((track + 11.25) / 22.5) % 16
    return f"{directions[idx]} ({int(track)}°)"

def get_location_name(lat, lon):
    if lat is None or lon is None:
        return "位置情報なし", None
    
    coord_str = f"({round(lat, 4)}, {round(lon, 4)})"
    google_maps_url = f"https://www.google.com/maps?q={lat},{lon}"

    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
        headers = {"User-Agent": "NationalHistoryTracker/5.0", "Accept-Language": "ja"}
        res = session.get(url, headers=headers, timeout=3).json()
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
            loc_str = f"📍 **{desc}**\n　└ [Googleマップで周辺地図を開く]({google_maps_url}) {coord_str}"
        else:
            loc_str = f"📍 [Googleマップで位置を確認]({google_maps_url}) {coord_str}"
        return loc_str, google_maps_url
    except:
        return f"📍 [Googleマップで位置を確認]({google_maps_url}) {coord_str}", google_maps_url

def is_near_japan(lat, lon):
    if lat is None or lon is None:
        return False
    return (JAPAN_LAT_MIN <= lat <= JAPAN_LAT_MAX) and (JAPAN_LON_MIN <= lon <= JAPAN_LON_MAX)

def is_target_aircraft(ac):
    own_op = str(ac.get("ownOp", "")).strip().lower()
    flight = str(ac.get("flight", "")).strip().lower()
    tail = str(ac.get("r", "")).strip().upper()

    if "N936CA" in tail:
        return True

    is_valid_op = any(op in own_op for op in ALLOWED_OPERATORS)
    is_valid_cs = any(cs in flight for cs in TARGET_CALLSIGNS)
    is_national_str = ("national" in own_op or "national" in flight or "ncr" in own_op or "ncr" in flight)

    return is_valid_op or is_valid_cs or is_national_str

def send_notification_async(tail, flight, ac_type, own_op, alt, track, speed, lat, lon, status_type, sighting_count):
    def _send():
        tail_str = tail if tail else "不明"
        flight_str = flight if flight else "不明"
        type_str = ac_type if ac_type else "B747 / 大型機"
        
        alt_str = "地上 (Ground)" if alt == "ground" or (isinstance(alt, (int, float)) and alt < 100) else f"{alt:,} ft" if isinstance(alt, (int, float)) else str(alt)
        speed_str = f"{speed} kts (約 {int(speed * 1.852)} km/h)" if isinstance(speed, (int, float)) else "不明"
        direction_str = get_direction_text(track)
        
        location_str, _ = get_location_name(lat, lon)
        near_japan = is_near_japan(lat, lon)
        is_n936ca = ("N936CA" in tail_str.upper())

        # 配色・演出ロジック
        if near_japan:
            embed_color = 0xFF0000  # 赤
            content_text = f"🇯🇵🚨 **【日本来航確定】ナショナル ({tail_str}) が日本の飛行・駐機エリアに到達しました！（ステータス: {status_type}）** @everyone"
            title_prefix = f"🇯🇵 【日本エリア到達】飛行ステータス ({status_type})"
        elif is_n936ca:
            embed_color = 0xF1C40F  # 黄色
            content_text = f"⭐✈️ **【注目機体 N936CA 検知】重要機体が動いています。（ステータス: {status_type}）**"
            title_prefix = f"⭐ 【N936CA 監視】{status_type}"
        elif "地上" in status_type:
            embed_color = 0xE67E22  # オレンジ
            content_text = f"🅿️ **【地上駐機】ナショナル ({tail_str}) がスポットに待機/到着しています。**"
            title_prefix = f"🅿️ 【地上駐機】ステータス ({status_type})"
        else:
            embed_color = 0x3498DB  # 青
            content_text = f"✈️ **【ナショナル運行情報】{tail_str} が {status_type} しました。**"
            title_prefix = f"✈️ 飛行ステータス ({status_type})"

        embed_data = {
            "title": title_prefix,
            "color": embed_color,
            "fields": [
                {"name": "🕒 検知時刻 (JST)", "value": get_jst_now_str(), "inline": False},
                {"name": "📍 位置情報・詳細", "value": location_str, "inline": False},
                {"name": "機体番号 (Tail)", "value": f"🌟 **{tail_str}**" if is_n936ca else tail_str, "inline": True},
                {"name": "機種 (Type)", "value": type_str, "inline": True},
                {"name": "コールサイン", "value": flight_str, "inline": True},
                {"name": "高度 / 速度", "value": f"{alt_str} / {speed_str}", "inline": True},
                {"name": "進行方位", "value": direction_str, "inline": True},
                {"name": "過去の観測回数", "value": f"📊 本システムでの通算観測: **{sighting_count} 回目**", "inline": True},
                {"name": "日本エリア判定", "value": "🔴 圏内（日本接近中/滞زام中）" if near_japan else "⚪ 海外・移動中", "inline": True},
            ],
            "footer": {"text": "National History-Enabled Tracker"}
        }

        payload = {
            "content": content_text,
            "embeds": [embed_data]
        }

        try:
            session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
            log_info(f"通知成功: Tail={tail_str}, ステータス={status_type}, 観測回数={sighting_count}")
        except Exception as e:
            log_error(f"Discord通知送信エラー: {e}")

    threading.Thread(target=_send, daemon=True).start()

def monitor_loop():
    global aircraft_states
    url = "https://api.adsb.lol/v2/mil"

    try:
        res = session.get(url, timeout=10).json()
        ac_list = res.get("ac", [])
        current_batch_icaos = set()

        for ac in ac_list:
            if not is_target_aircraft(ac):
                continue

            icao = ac.get("hex", "").strip()
            if not icao:
                continue

            current_batch_icaos.add(icao)

            tail = ac.get("r", "N/A").strip().upper()
            flight = ac.get("flight", "N/A").strip()
            ac_type = ac.get("t", ac.get("desc", "不明")).strip()
            own_op = ac.get("ownOp", "不明").strip()
            alt = ac.get("alt_baro")
            track = ac.get("track")
            speed = ac.get("speed")
            lat = ac.get("lat")
            lon = ac.get("lon")

            is_ground = (alt == "ground") or (isinstance(alt, (int, float)) and alt < 100)
            is_currently_in_air = not is_ground
            near_japan = is_near_japan(lat, lon)

            # 初回検知または状態変化、日本エリア突入時
            if icao not in aircraft_states:
                status_type = "地上駐機・スポットイン" if is_ground else "新規空中検知"
                
                # 履歴ファイルに記録して通算回数を取得
                sighting_count = record_flight_event(tail, flight, ac_type, status_type, lat, lon)
                log_info(f"【新規発見・履歴記録】 Tail: {tail} | 状態: {status_type} | 通算: {sighting_count}回目")
                
                aircraft_states[icao] = {
                    "in_air": is_currently_in_air,
                    "last_status": status_type,
                    "tail": tail,
                    "near_japan": near_japan
                }
                send_notification_async(tail, flight, ac_type, own_op, alt, track, speed, lat, lon, status_type, sighting_count)
            else:
                last_state = aircraft_states[icao]
                last_in_air = last_state["in_air"]
                last_near_japan = last_state["near_japan"]

                if last_in_air != is_currently_in_air:
                    status_type = "離陸 (Takeoff)" if is_currently_in_air else "着陸・スポットイン (Landing)"
                    sighting_count = record_flight_event(tail, flight, ac_type, status_type, lat, lon)
                    log_info(f"【状態変化】 Tail: {tail} が {status_type} しました。")
                    send_notification_async(tail, flight, ac_type, own_op, alt, track, speed, lat, lon, status_type, sighting_count)
                    
                    aircraft_states[icao]["in_air"] = is_currently_in_air
                    aircraft_states[icao]["last_status"] = status_type

                if not last_near_japan and near_japan:
                    status_type = "日本エリア突入"
                    sighting_count = record_flight_event(tail, flight, ac_type, status_type, lat, lon)
                    log_info(f"【日本エリア突入】 Tail: {tail} が日本の領域に入りました！")
                    send_notification_async(tail, flight, ac_type, own_op, alt, track, speed, lat, lon, status_type, sighting_count)
                    aircraft_states[icao]["near_japan"] = near_japan

    except Exception as e:
        log_error(f"APIチェック中エラー: {e}")

if __name__ == "__main__":
    log_info("ナショナル・エアラインズ 履歴学習型監視システムを開始しました...")
    send_startup_notification()
    
    while True:
        monitor_loop()
        time.sleep(CHECK_INTERVAL)
