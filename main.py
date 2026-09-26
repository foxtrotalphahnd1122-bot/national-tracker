import os
os.environ["TZ"] = "Asia/Tokyo"
import time
import math
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# === ネットワークセッションの持続化（高速化用） ===
session = requests.Session()

# === Renderのポート検知をクリア＆501エラー解消用ダミーサーバー ===
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
        return  # サーバーアクセスの標準ログを抑制

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

# === 設定項目 ===
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FLIGHTAWARE_API_KEY = os.environ.get("FLIGHTAWARE_API_KEY")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))

JAPAN_AIRPORT_PREFIXES = ("RJ", "RO")

# 1. 許可する運用者（ナショナル・エアラインズ関連のみ）
ALLOWED_OPERATORS = [
    "national air", "national airlines", "ncr"
]

# 2. 完全除外するキーワード（他の軍やチャーターをすべて弾く）
EXCLUDE_OPERATORS = [
    "air force", "usaf", "navy", "usn", "marine", "usmc", "army", "coast guard", 
    "omega", "rch", "reach"
]

# 3. ナショナル・エアラインズ特有のコールサインや識別子
TARGET_CALLSIGNS = [
    "national", "ncr"
]

# 4. 主力の大型機材コード
TARGET_TYPES = [
    "b744", "b748", "b74f", "b747", "boeing 747", "boeing747", "b77f", "a332", "a333"
]

EXCLUDE_TYPES = [
    "cessna", "c172", "c-172", "c150", "c-150", "c152", "c-152", "c182", "c-182", "c208", "c-208",
    "ec35", "ec-35", "ec38", "ec130", "ec145", "as350", "as355", "h125", "h130", "h135", "h145",
    "a400", "a-400", "a400m", "h60", "h-60", "mh60", "sh60", "hh60", "uh60"
]

if not DISCORD_WEBHOOK_URL:
    raise ValueError("エラー: DISCORD_WEBHOOK_URL が設定されていません。")

in_air_states = {}
notified_icaos = set()
low_altitude_notified = set()


def get_jst_now_str():
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime('%Y-%m-%d %H:%M:%S (JST)')


def log_info(message):
    print(f"🟢 [INFO] [{get_jst_now_str()}] {message}")

def log_warn(message):
    print(f"⚠️ [WARN] [{get_jst_now_str()}] {message}")

def log_error(message):
    print(f"❌ [ERROR] [{get_jst_now_str()}] {message}")

def log_alert(message):
    print(f"🚨 [ALERT] [{get_jst_now_str()}] {message}")


def send_startup_notification():
    payload = {
        "content": "✨ **【システム起動成功】ナショナル特化型トラッカー（N936CA特別警戒＆日本行き強調機能付き）が稼働を開始しました！**",
        "embeds": [{
            "title": "🚀 起動・接続テスト (National Dedicated + N936CA Watcher)",
            "color": 0x2ECC71,
            "fields": [
                {"name": "ステータス", "value": f"高速ポーリング({CHECK_INTERVAL}秒)・N936CA特別監視・日本行きルート自動強調稼働中", "inline": True},
                {"name": "時刻", "value": get_jst_now_str(), "inline": True},
            ],
            "footer": {"text": "National Airlines Tracker - Advanced Alert Edition"}
        }]
    }
    try:
        session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        log_info("起動テスト通知の送信に成功しました。")
    except Exception as e:
        log_error(f"起動テスト送信エラー: {e}")


def is_target_aircraft(ac):
    lat = ac.get("lat")
    lon = ac.get("lon")
    alt = ac.get("alt_baro")
    if lat is None or lon is None or alt is None:
        return False

    ac_type = str(ac.get("t", "")).strip().lower().replace(" ", "").replace("-", "")
    desc = str(ac.get("desc", "")).strip().lower()
    own_op = str(ac.get("ownOp", "")).strip().lower()
    flight = str(ac.get("flight", "")).strip().lower()
    tail = str(ac.get("r", "")).strip().upper()

    # N936CA なら無条件で最優先ターゲットに指定
    if "N936CA" in tail:
        return True

    for ex_op in EXCLUDE_OPERATORS:
        if ex_op in own_op:
            return False

    for exclude in EXCLUDE_TYPES:
        exclude_clean = exclude.replace("-", "")
        if (exclude in ac_type or exclude_clean in ac_type or
            exclude in desc or exclude_clean in desc):
            return False

    is_target_callsign = any(cs in flight for cs in TARGET_CALLSIGNS)
    is_valid_operator = any(op in own_op for op in ALLOWED_OPERATORS)
    is_national_related = ("national" in own_op or "national" in flight or "ncr" in own_op or "ncr" in flight)

    if is_valid_operator or is_target_callsign or is_national_related:
        return True

    return False


def get_location_info(lat, lon):
    if lat is None or lon is None:
        return "位置情報なし", None
    
    coord_str = f"({round(lat, 4)}, {round(lon, 4)})"
    google_maps_url = f"https://www.google.com/maps?q={lat},{lon}"
    static_map_url = f"https://static-maps.yandex.ru/1.x/?ll={lon},{lat}&z=9&size=650,300&l=map&lang=ja_JP&pt={lon},{lat},pm2rdl"

    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
        headers = {"User-Agent": "National-Airlines-Tracker/2.0", "Accept-Language": "ja"}
        res = session.get(url, headers=headers, timeout=3).json()
        
        address = res.get("address", {})
        country = address.get("country", "")
        state = address.get("state", "")
        city = address.get("city") or address.get("town") or address.get("village") or address.get("county", "")
        suburb = address.get("suburb") or address.get("neighbourhood", "")
        aerodrome = address.get("aerodrome") or address.get("military") or address.get("aeroway", "")

        location_parts = []
        if aerodrome:
            location_parts.append(f"施設/空港: {aerodrome}")
        if country:
            location_parts.append(f"国: {country}")
        if state:
            location_parts.append(f"地域: {state}")
        if city:
            location_parts.append(f"市区町村: {city}")
        if suburb:
            location_parts.append(f"地区: {suburb}")

        if location_parts:
            text_desc = " / ".join(location_parts)
            location_str = f"📍 **{text_desc}**\n　└ [Googleマップで周辺地図を開く]({google_maps_url}) {coord_str}"
        else:
            location_str = f"📍 [周辺の特定地名なし - Googleマップで開く]({google_maps_url}) {coord_str}"

        return location_str, static_map_url

    except Exception as e:
        fallback_str = f"📍 [Googleマップで位置を確認]({google_maps_url}) {coord_str}"
        return fallback_str, static_map_url


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


def is_destination_japan_airport(destination_str):
    if not destination_str or destination_str in ["不明", "N/A"]:
        return False
    dest_clean = destination_str.strip().upper()
    return dest_clean.startswith(JAPAN_AIRPORT_PREFIXES)


def get_flight_route(flight_number):
    if not FLIGHTAWARE_API_KEY or not flight_number or flight_number == "N/A":
        return "不明", "不明"

    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{flight_number.strip()}"
    headers = {"x-apikey": FLIGHTAWARE_API_KEY}

    try:
        res = session.get(url, headers=headers, timeout=4)
        if res.status_code == 200:
            data = res.json()
            flights = data.get("flights", [])
            if flights:
                latest = flights[0]
                origin = (latest.get("origin") or {}).get("code") or "不明"
                destination = (latest.get("destination") or {}).get("code") or "不明"
                return origin, destination
    except Exception as e:
        pass

    return "不明", "不明"


# === ルート事前調査＆条件別ハイライト通知 ===
def send_discord_notification_async(icao, tail, flight, ac_type, own_op, alt, track, location_str, map_image_url, event_type):
    def _send():
        flight_str = flight if flight else "不明"
        tail_str = tail if tail else "不明"
        type_str = ac_type if ac_type else "B747 / 大型機"
        op_str = own_op if own_op else "National Airlines"
        
        # 1. FlightAware API でフライト情報を事前調査
        fa_origin, destination = get_flight_route(flight_str)
        origin = fa_origin if fa_origin != "不明" else location_str

        direction_str = get_direction_text(track)
        is_japan_bound = is_destination_japan_airport(destination)
        is_n936ca = ("N936CA" in tail_str.upper())
        is_low_alt_alert = (event_type == "降下・着陸警戒")

        # 2. 状態に応じた通知文言とカラーの決定
        if is_n936ca:
            embed_color = 0x900C3F  # 濃い赤（最重要特別警戒）
            content_text = f"🚨🔥 **【超重要・特別警戒】ナショナル・エアラインズの注目機体 【 N936CA 】 が検知されました！！** @everyone"
            title_prefix = "🔥 【特別監視】重要機体 N936CA 検出"
        elif is_japan_bound:
            embed_color = 0xFF0000  # 赤（日本行き判明）
            content_text = f"🇯🇵🚨 **【日本行き判明】ナショナル・エアラインズ ({tail_str}) の目的地が「日本の空港 ({destination})」に設定されています！** @everyone"
            title_prefix = f"🇯🇵 【日本行き確定】飛行ステータス ({event_type})"
        elif is_low_alt_alert:
            embed_color = 0xE67E22  # オレンジ
            content_text = f"🛬 **【着陸警戒】ナショナル ({tail_str}) が高度 7,500 ft 未満 ({alt} ft) に降下しました。** ⚠️"
            title_prefix = f"🛬 【着陸警戒】飛行ステータス"
        else:
            embed_color = 0x3498DB  # 青
            content_text = f"✈️ **【ナショナル{event_type}】{type_str}: {tail_str} (Callsign: {flight_str})**"
            title_prefix = f"✈️ ナショナル・エアラインズ 飛行ステータス ({event_type})"

        embed_data = {
            "title": f"{title_prefix}",
            "color": embed_color,
            "fields": [
                {"name": "🕒 通過時刻 (日本時間)", "value": get_jst_now_str(), "inline": False},
                {"name": "📍 現在地（地図リンク・周辺情報）", "value": location_str, "inline": False},
                {"name": "機体番号 (Tail / Reg)", "value": f"🌟 **{tail_str}**" if is_n936ca else tail_str, "inline": True},
                {"name": "機体型式 (Type)", "value": type_str, "inline": True},
                {"name": "フライト番号 (Callsign)", "value": flight_str, "inline": True},
                {"name": "所属/運用者 (Operator)", "value": op_str, "inline": True},
                {"name": "高度", "value": f"{alt} ft" if isinstance(alt, (int, float)) else str(alt), "inline": True},
                {"name": "🧭 進行方位（向き）", "value": direction_str, "inline": True},
                {"name": "🛫 出発地 (事前調査済)", "value": origin, "inline": True},
                {"name": "🛬 目的地 (事前調査済)", "value": f"🚨 **{destination} (日本国内空港)**" if is_japan_bound else destination, "inline": True},
            ],
            "footer": {"text": "National Airlines Dedicated Tracker - N936CA Watcher Enabled"}
        }

        if map_image_url:
            embed_data["image"] = {"url": map_image_url}

        payload = {
            "content": content_text,
            "embeds": [embed_data]
        }

        try:
            session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
            log_info(f"Discord通知送信成功 [{event_type}]: Tail={tail_str}, 目的地={destination}, 日本行き={is_japan_bound}, N936CA={is_n936ca}")
        except Exception as e:
            log_error(f"Discord送信エラー: {e}")

    threading.Thread(target=_send, daemon=True).start()


def check_military_takeoff():
    global in_air_states, notified_icaos, low_altitude_notified

    url = "https://api.adsb.lol/v2/mil"
    try:
        res = session.get(url, timeout=10).json()
        ac_list = res.get("ac", [])
        if not ac_list:
            return

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
            lat = ac.get("lat")
            lon = ac.get("lon")

            is_ground = (alt == "ground") or (isinstance(alt, (int, float)) and alt < 100)
            is_in_air_current = not is_ground
            is_in_air_last = in_air_states.get(icao)

            is_takeoff = (is_in_air_last is False and is_in_air_current is True)
            is_new_detection = (icao not in notified_icaos and is_in_air_current)

            # 1. 新規検知または離陸の通知
            if is_takeoff or is_new_detection:
                event_type = "離陸" if is_takeoff else "検知"
                log_info(f"【ナショナル新規{event_type}】 機種: {ac_type} | Tail: {tail} | Callsign: {flight} | 高度: {alt}ft")
                
                location_str, map_image_url = get_location_info(lat, lon)
                
                send_discord_notification_async(icao, tail, flight, ac_type, own_op, alt, track, location_str, map_image_url, event_type)
                
                notified_icaos.add(icao)

            # 2. 高度 7,500 ft 未満降下時の着陸警戒通知
            if is_in_air_current and isinstance(alt, (int, float)) and alt <= 7500:
                if icao not in low_altitude_notified:
                    log_alert(f"【降下警戒】ナショナル機 高度低下: {alt} ft (Tail: {tail})")
                    location_str, map_image_url = get_location_info(lat, lon)
                    
                    send_discord_notification_async(icao, tail, flight, ac_type, own_op, alt, track, location_str, map_image_url, "降下・着陸警戒")
                    low_altitude_notified.add(icao)
            elif isinstance(alt, (int, float)) and alt > 8500:
                if icao in low_altitude_notified:
                    low_altitude_notified.remove(icao)

            in_air_states[icao] = is_in_air_current

        notified_icaos = notified_icaos.intersection(current_batch_icaos)
        low_altitude_notified = low_altitude_notified.intersection(current_batch_icaos)

    except Exception as e:
        log_error(f"APIチェック中エラー: {e}")


if __name__ == "__main__":
    log_info("ナショナル・エアラインズ特化型トラッカー（N936CA監視対応）システムを開始しました...")
    
    send_startup_notification()
    
    while True:
        time.sleep(CHECK_INTERVAL)
        check_military_takeoff()
