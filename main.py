import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# === 1. Render無料枠維持用ダミーサーバー ===
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

# === 2. 設定項目 ===
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FLIGHTAWARE_API_KEY = os.environ.get("FLIGHTAWARE_API_KEY")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))

JAPAN_AIRPORT_PREFIXES = ("RJ", "RO")
NATIONAL_CALLSIGNS = ("NCR", "N8")

if not DISCORD_WEBHOOK_URL:
    raise ValueError("エラー: DISCORD_WEBHOOK_URL が設定されていません。")

in_air_states = {}


def is_national_airlines(flight_str, desc_str):
    """National Airlines の機体かどうか判定"""
    flight_clean = flight_str.strip().upper()
    desc_clean = desc_str.strip().upper()

    if flight_clean.startswith(NATIONAL_CALLSIGNS):
        return True
    if "NATIONAL AIRLINES" in desc_clean or "NATIONAL" in desc_clean:
        return True
    return False


def get_nearest_airport(lat, lon):
    """位置座標から最寄りの地域を取得"""
    if lat is None or lon is None:
        return "不明"
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
        headers = {"User-Agent": "ADSB-National-Tracker/1.0"}
        res = requests.get(url, headers=headers, timeout=5).json()
        
        address = res.get("address", {})
        aeroway = address.get("aeroway") or address.get("military")
        
        if aeroway:
            return str(aeroway)
        
        location_name = (address.get("aerodrome") or 
                         address.get("city") or 
                         address.get("town") or 
                         address.get("county") or 
                         address.get("state") or "付近")
        return f"{location_name} 周辺"
    except Exception:
        return f"座標 ({round(lat, 2)}, {round(lon, 2)})"


def get_direction_text(track):
    """進行方位を16方位表記に変換"""
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


def send_discord_notification(icao, tail, flight, ac_type, alt, track, origin, destination):
    flight_str = flight if flight else "不明"
    tail_str = tail if tail else "不明"
    type_str = ac_type if ac_type else "National Airlines"
    direction_str = get_direction_text(track)
    is_japan_airport = is_destination_japan_airport(destination)

    if is_japan_airport:
        content_text = f"🚨🚨 **【日本飛来警告】National Airlines {type_str} ({flight_str}) の目的地が「日本の空港 ({destination})」に設定されました！** @everyone"
        embed_color = 15158332
        card_title = "🚨 🚨 National Airlines 日本飛来（離陸検知）"
    else:
        content_text = f"✈️ **【National Airlines】離陸検知: {type_str} ({flight_str})**"
        embed_color = 3447003
        card_title = "✈️ National Airlines 離陸ステータス"

    payload = {
        "content": content_text,
        "embeds": [
            {
                "title": card_title,
                "color": embed_color,
                "fields": [
                    {"name": "航空会社", "value": "National Airlines (NCR)", "inline": True},
                    {"name": "機体型式 (Type)", "value": type_str, "inline": True},
                    {"name": "フライト番号 (Callsign)", "value": flight_str, "inline": True},
                    {"name": "機体番号 (Tail / Reg)", "value": tail_str, "inline": True},
                    {"name": "ICAOコード", "value": icao.upper(), "inline": True},
                    {"name": "高度", "value": f"{alt} ft", "inline": True},
                    {"name": "🧭 進行方位", "value": direction_str, "inline": True},
                    {"name": "🛫 出発地（最寄り）", "value": origin, "inline": True},
                    {"name": "🛬 目的地", "value": destination, "inline": True},
                ],
                "footer": {"text": "ADSB National Airlines Tracker"}
            }
        ]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[{time.strftime('%H:%M:%S')}] Discord通知送信完了: {flight_str}")
    except Exception as e:
        print(f"送信エラー: {e}")


def check_national_takeoff():
    global in_air_states

    url = "https://api.adsb.lol/v2/pia"
    try:
        res = requests.get(url, timeout=15).json()
        ac_list = res.get("ac", [])
        if not ac_list:
            return

        for ac in ac_list:
            flight = ac.get("flight", "N/A").strip()
            desc = ac.get("desc", "").strip()

            if not is_national_airlines(flight, desc):
                continue

            icao = ac.get("hex", "").strip()
            if not icao:
                continue

            tail = ac.get("r", "N/A").strip()
            ac_type = ac.get("t", desc if desc else "B747/B757").strip()
            alt = ac.get("alt_baro")
            track = ac.get("track")
            lat = ac.get("lat")
            lon = ac.get("lon")

            is_ground = (alt == "ground") or (isinstance(alt, (int, float)) and alt < 100)
            is_in_air_current = not is_ground

            is_in_air_last = in_air_states.get(icao)

            if is_in_air_last is False and is_in_air_current is True:
                print(f"離陸検知！ フライト: {flight}, 機種: {ac_type}")
                fa_origin, destination = get_flight_route(flight)
                origin = fa_origin if fa_origin != "不明" else get_nearest_airport(lat, lon)
                send_discord_notification(icao, tail, flight, ac_type, alt, track, origin, destination)

            in_air_states[icao] = is_in_air_current

    except Exception as e:
        print(f"チェック中エラー: {e}")


if __name__ == "__main__":
    print("National Airlines (全便) の常時監視を開始しました...")
    while True:
        check_national_takeoff()
        time.sleep(CHECK_INTERVAL)
