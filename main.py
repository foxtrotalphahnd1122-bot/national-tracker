import os
import time
import math
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# === 1. Renderのポート検知をクリア＆501エラー解消用ダミーサーバー ===
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
        return  # ログ無効化（UptimeRobotのアクセスによるログの汚れを防ぐ）

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# バックグラウンドスレッドで常時ダミーサーバーを稼働させスリープを防止
threading.Thread(target=start_dummy_server, daemon=True).start()

# === 2. 設定項目 ===
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FLIGHTAWARE_API_KEY = os.environ.get("FLIGHTAWARE_API_KEY")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))

JAPAN_AIRPORT_PREFIXES = ("RJ", "RO")

# 1. 最優先監視対象（特定レジ番）
PRIORITY_TAIL = "N936CA"

# 2. National Airlines 識別キーワード（すべて大文字で比較）
NATIONAL_OPERATORS = [
    "NATIONAL AIRLINES", "NATIONAL AIR CARGO", "NATIONAL CARGO", "NATIONAL AIR"
]

# 3. ADS-B（adsb.lol）表記に対応した指定機種コード（すべて大文字表記）
NATIONAL_TARGET_TYPES = [
    "B744", "B748", "B742", "B741", "B74F", "B74D", "B747", "B-747",
    "A332", "A333", "A339", "A330", "A-330",
    "B752", "B753", "B757", "B-757",
    "B762", "B763", "B764", "B767", "B-767"
]

if not DISCORD_WEBHOOK_URL:
    raise ValueError("エラー: DISCORD_WEBHOOK_URL が設定されていません。")

# 機体の状態管理
in_air_states = {}
notified_icaos = set()


def send_startup_notification():
    """Live化した瞬間に1回だけ送る接続テスト通知"""
    payload = {
        "content": "🚀 **【システム起動成功】National Airlines 専用監視プログラムがLive化しました！**",
        "embeds": [{
            "title": "🚀 起動・接続テスト",
            "color": 0x2ECC71,
            "fields": [
                {"name": "監視対象", "value": f"National Airlines (最優先: {PRIORITY_TAIL})", "inline": True},
                {"name": "更新間隔", "value": f"{CHECK_INTERVAL}秒", "inline": True},
                {"name": "時刻", "value": time.strftime('%H:%M:%S'), "inline": True},
            ],
            "footer": {"text": "National Airlines Priority Tracker"}
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[{time.strftime('%H:%M:%S')}] 起動直後テスト通知の送信成功！")
    except Exception as e:
        print(f"起動テスト送信エラー: {e}")


def is_national_airlines(ac):
    """National Airlines 機体（および N936CA）の判定 (大文字・小文字完全吸収 / RCH除外)"""
    tail = str(ac.get("r", "")).strip().upper()
    own_op = str(ac.get("ownOp", "")).strip().upper()
    flight = str(ac.get("flight", "")).strip().upper()
    ac_type = str(ac.get("t", "")).strip().upper().replace(" ", "")
    desc = str(ac.get("desc", "")).strip().upper()

    # --- 0. RCH / REACH フライトの完全除外 ---
    if flight.startswith("RCH") or flight.startswith("REACH"):
        return False

    # --- 1. 【最優先】特定機体 N936CA は即対象 ---
    if tail == PRIORITY_TAIL:
        return True

    # --- 2. 判定要素の抽出 ---
    is_ncr_callsign = flight.startswith("NCR")
    is_national_op = any(op in own_op for op in NATIONAL_OPERATORS)
    
    # 機種コードの一致判定（大文字比較）
    is_target_type = False
    for target in NATIONAL_TARGET_TYPES:
        target_clean = target.replace("-", "")
        if (target == ac_type or target_clean == ac_type or
            target in desc or target_clean in desc):
            is_target_type = True
            break

    # --- 3. 総合判定 ---
    if (is_ncr_callsign or is_national_op) and is_target_type:
        return True

    if is_ncr_callsign:
        return True

    return False


def get_location_name(lat, lon):
    """緯度・経度から地名・施設名を取得"""
    if lat is None or lon is None:
        return "位置情報なし"
    
    coord_str = f"({round(lat, 4)}, {round(lon, 4)})"
    
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
        headers = {"User-Agent": "National-Airlines-Tracker/1.0"}
        res = requests.get(url, headers=headers, timeout=5).json()
        
        address = res.get("address", {})
        aeroway = address.get("aeroway") or address.get("military")
        
        if aeroway:
            return f"{aeroway} 周辺 {coord_str}"
        
        location_name = (address.get("aerodrome") or 
                         address.get("city") or 
                         address.get("town") or 
                         address.get("county") or 
                         address.get("state") or "")
        
        if location_name:
            return f"{location_name} 上空/周辺 {coord_str}"
        return f"座標 {coord_str}"
    except Exception:
        return f"座標 {coord_str}"


def get_direction_text(track):
    """方位角を16方位に変換"""
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


def is_japan_airport(airport_code):
    """空港コードが日本のもの（RJ/RO始まり）か判定"""
    if not airport_code or airport_code in ["不明", "N/A"]:
        return False
    code_clean = airport_code.strip().upper()
    return code_clean.startswith(JAPAN_AIRPORT_PREFIXES)


def get_flight_route(flight_number):
    if not FLIGHTAWARE_API_KEY or not flight_number or flight_number == "N/A":
        return "不明", "不明"

    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{flight_number.strip()}"
    headers = {"x-apikey": FLIGHTAWARE_API_KEY}

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            flights = data.get("flights", [])
            if flights:
                latest = flights[0]
                origin = (latest.get("origin") or {}).get("code") or "不明"
                destination = (latest.get("destination") or {}).get("code") or "不明"
                return origin, destination
    except Exception as e:
        print(f"目的地取得エラー: {e}")

    return "不明", "不明"


def send_discord_notification(icao, tail, flight, ac_type, own_op, alt, track, origin, destination, location_str, event_type="検知"):
    flight_str = flight if flight else "不明"
    tail_str = tail if tail else "不明"
    type_str = ac_type if ac_type else "National Airlines 機"
    op_str = own_op if own_op else "National Airlines"
    direction_str = get_direction_text(track)
    
    is_priority_aircraft = (tail_str.upper() == PRIORITY_TAIL)
    is_dest_japan = is_japan_airport(destination)
    is_origin_japan = is_japan_airport(origin)
    is_japan_related = is_dest_japan or is_origin_japan

    if is_priority_aircraft and is_japan_related:
        content_text = f"🚨🚨🚨 **【超緊急・特別警戒】最重要警戒機 {PRIORITY_TAIL} が日本路線（出発: {origin} / 目的: {destination}）で{event_type}されました！！** @everyone"
        embed_color = 0x990000
        title_prefix = f"🔥【超極秘/最優先機・日本便】"
    elif is_priority_aircraft:
        content_text = f"📦 **【最優先機検知】{PRIORITY_TAIL} (National Airlines) が{event_type}されました**"
        embed_color = 0xE67E22
        title_prefix = f"⚠️【最優先機・海外路線】"
    elif is_dest_japan:
        content_text = f"🚨 **【目的地注意】National Airlines {type_str} ({tail_str}) の目的地が「日本の空港 ({destination})」です！** @everyone"
        embed_color = 0xFF0000
        title_prefix = f"🇯🇵【日本便検知】"
    else:
        content_text = f"📦 **【National Airlines {event_type}】{type_str}: {tail_str} (Callsign: {flight_str})**"
        embed_color = 0x3498DB
        title_prefix = f"📦"

    payload = {
        "content": content_text,
        "embeds": [
            {
                "title": f"{title_prefix} National Airlines 飛行ステータス ({event_type})",
                "color": embed_color,
                "fields": [
                    {"name": "機体番号 (Tail / Reg)", "value": f"**{tail_str}**", "inline": True},
                    {"name": "機体型式 (Type)", "value": type_str, "inline": True},
                    {"name": "フライト番号 (Callsign)", "value": flight_str, "inline": True},
                    {"name": "所属/運用者 (Operator)", "value": op_str, "inline": True},
                    {"name": "ICAOコード", "value": icao.upper(), "inline": True},
                    {"name": "高度", "value": f"{alt} ft" if isinstance(alt, (int, float)) else str(alt), "inline": True},
                    {"name": "🧭 進行方位（向き）", "value": direction_str, "inline": True},
                    {"name": "📍 反応位置（地名＆座標）", "value": location_str, "inline": False},
                    {"name": "🛫 出発地", "value": f"**{origin}**", "inline": True},
                    {"name": "🛬 目的地", "value": f"**{destination}**", "inline": True},
                ],
                "footer": {"text": "National Airlines Priority Tracker"}
            }
        ]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[{time.strftime('%H:%M:%S')}] Discord通知完了({event_type}): {type_str} {tail_str} ({flight_str})")
    except Exception as e:
        print(f"送信エラー: {e}")


def check_national_airlines():
    global in_air_states, notified_icaos

    url = "https://api.adsb.lol/v2/mil"
    try:
        res = requests.get(url, timeout=15).json()
        ac_list = res.get("ac", [])
        if not ac_list:
            return

        current_batch_icaos = set()

        for ac in ac_list:
            if not is_national_airlines(ac):
                continue

            icao = ac.get("hex", "").strip()
            if not icao:
                continue

            current_batch_icaos.add(icao)

            tail = ac.get("r", "N/A").strip()
            flight = ac.get("flight", "N/A").strip()
            ac_type = ac.get("t", ac.get("desc", "不明")).strip()
            own_op = ac.get("ownOp", "National Airlines").strip()
            alt = ac.get("alt_baro")
            track = ac.get("track")
            lat = ac.get("lat")
            lon = ac.get("lon")

            is_ground = (alt == "ground") or (isinstance(alt, (int, float)) and alt < 100)
            is_in_air_current = not is_ground
            is_in_air_last = in_air_states.get(icao)

            is_takeoff = (is_in_air_last is False and is_in_air_current is True)
            is_new_detection = (icao not in notified_icaos and is_in_air_current)

            if is_takeoff or is_new_detection:
                event_type = "離陸" if is_takeoff else "検知"
                print(f"【{event_type}】 National Airlines - 機種: {ac_type}, Tail: {tail}, Flight: {flight}")
                
                location_str = get_location_name(lat, lon)
                fa_origin, destination = get_flight_route(flight)
                origin = fa_origin if fa_origin != "不明" else location_str
                
                send_discord_notification(icao, tail, flight, ac_type, own_op, alt, track, origin, destination, location_str, event_type)
                
                notified_icaos.add(icao)

            in_air_states[icao] = is_in_air_current

        notified_icaos = notified_icaos.intersection(current_batch_icaos)

    except Exception as e:
        print(f"チェック中エラー: {e}")


if __name__ == "__main__":
    print("National Airlines 専用監視システムを開始しました...")
    
    # Liveになった瞬間に起動テスト通知を即座に送信
    send_startup_notification()
    
    # 以降は監視ループ
    while True:
        time.sleep(CHECK_INTERVAL)
        check_national_airlines()
