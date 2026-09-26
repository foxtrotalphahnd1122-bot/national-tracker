import os
import time
import math
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

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
        return  # ログをキレイに保つため無効化

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

# === 設定項目 ===
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
FLIGHTAWARE_API_KEY = os.environ.get("FLIGHTAWARE_API_KEY")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))

JAPAN_AIRPORT_PREFIXES = ("RJ", "RO")

# ==========================================
# 【Aグループ】米軍・特殊機・オメガ空中給油機の設定
# ==========================================
MIL_ALLOWED_OPERATORS = [
    "us air force", "usaf", "united states air force",
    "us navy", "usn", "united states navy",
    "us marine corps", "usmc", "united states marine corps",
    "omega air", "omega aerial refueling", "omega tanker"
]

MIL_TARGET_CALLSIGNS = [
    "sentry",
    "recon", "jake", "cobra", "snoop", "bolt", "pyton", "olay",
    "hobo", "gold", "ethyl", "teal", "qid", "m35", "boeing"
]

MIL_TARGET_TYPES = [
    "k35r", "k35q", "c135", "c35", "kc135", "kc-135", "nc135", "tc135",
    "r135", "rc135", "rc-135",
    "w135", "wc135", "wc-135", "wc135w", "wc135c",
    "ec135", "ec-135", "oc135", "oc-135",
    "e3tf", "e3cf", "e3a", "e3b", "e3c", "e3d", "e3g", "e3", "e-3", "sentry",
    "e4", "e4b", "e-4b", "vc25", "vc25a", "vc-25a", "vc25b", "vc-25b",
    "b707", "b-707", "b703", "b-703", "boeing707", "boeing 707",
    "e6", "e-6", "e6b", "e-6b", "e8", "e-8", "e8c", "e-8c", "c137", "c-137",
    "kdc10", "kdc-10", "dc10", "dc-10", "dc103"
]

MIL_EXCLUDE_TYPES = [
    "e390", "e-390", "kc390", "kc-390", "c390", "c-390",
    "h60", "h-60", "mh60", "mh-60", "sh60", "sh-60", "hh60", "hh-60", "uh60", "uh-60",
    "blackhawk", "seahawk", "jayhawk", "knighthawk"
]

# ==========================================
# 【Bグループ】National Airlines の設定
# ==========================================
PRIORITY_TAIL = "N936CA"

NATIONAL_OPERATORS = [
    "NATIONAL AIRLINES", "NATIONAL AIR CARGO", "NATIONAL CARGO", "NATIONAL AIR"
]

NATIONAL_TARGET_TYPES = [
    "B744", "B748", "B742", "B741", "B74F", "B74D", "B747", "B-747",
    "A332", "A333", "A339", "A330", "A-330",
    "B752", "B753", "B757", "B-757",
    "B762", "B763", "B764", "B767", "B-767"
]

if not DISCORD_WEBHOOK_URL:
    raise ValueError("エラー: DISCORD_WEBHOOK_URL が設定されていません。")

# 機体の状態管理
mil_in_air_states = {}
mil_notified_icaos = set()

nat_in_air_states = {}
nat_notified_icaos = set()


def send_startup_notification():
    """起動成功通知"""
    payload = {
        "content": "🚀 **【システム統合起動成功】米軍・オメガ機 ＆ National Airlines 監視プログラムがLive化しました！**",
        "embeds": [{
            "title": "🚀 起動・接続テスト (デュアル監視)",
            "color": 0x2ECC71,
            "fields": [
                {"name": "監視グループ 1", "value": "米軍特殊機・オメガ空中給油機", "inline": True},
                {"name": "監視グループ 2", "value": f"National Airlines (優先: {PRIORITY_TAIL})", "inline": True},
                {"name": "更新間隔", "value": f"{CHECK_INTERVAL}秒", "inline": True},
            ],
            "footer": {"text": "ADSB Military & National Tracker"}
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[{time.strftime('%H:%M:%S')}] 統合起動テスト通知の送信成功！")
    except Exception as e:
        print(f"起動テスト送信エラー: {e}")


def get_location_name(lat, lon):
    if lat is None or lon is None:
        return "位置情報なし"
    
    coord_str = f"({round(lat, 4)}, {round(lon, 4)})"
    
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
        headers = {"User-Agent": "ADSB-Dual-Tracker/1.0"}
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


# ==========================================
# 判定ロジック (米軍機・オメガ機)
# ==========================================
def is_target_military_aircraft(ac):
    ac_type = str(ac.get("t", "")).strip().lower().replace(" ", "")
    desc = str(ac.get("desc", "")).strip().lower()
    own_op = str(ac.get("ownOp", "")).strip().lower()
    flight = str(ac.get("flight", "")).strip().lower()

    if "national air" in own_op or flight.startswith("ncr") or flight.startswith("rch") or flight.startswith("reach"):
        return False

    for exclude in MIL_EXCLUDE_TYPES:
        exclude_clean = exclude.replace("-", "")
        if (exclude in ac_type or exclude_clean in ac_type or
            exclude in desc or exclude_clean in desc):
            return False

    is_target_callsign = any(cs in flight for cs in MIL_TARGET_CALLSIGNS)

    is_allowed_op = False
    if own_op:
        is_allowed_op = any(op in own_op for op in MIL_ALLOWED_OPERATORS)
    else:
        is_allowed_op = True

    desc_clean = desc.replace(" ", "").replace("-", "")
    is_target_type = False

    for target in MIL_TARGET_TYPES:
        target_clean = target.replace("-", "")
        if (target == ac_type or target_clean == ac_type or
            target in desc or target_clean in desc_clean):
            is_target_type = True
            break

    if not is_target_type:
        if any(k in ac_type or k in desc for k in ["135", "w135", "sentry", "boeing707", "b707", "kdc10", "vc25", "e-3", "e-4", "e-6", "e-8"]):
            is_target_type = True

    if is_target_callsign:
        return True

    if is_allowed_op and is_target_type:
        return True

    return False


# ==========================================
# 判定ロジック (National Airlines)
# ==========================================
def is_national_airlines(ac):
    tail = str(ac.get("r", "")).strip().upper()
    own_op = str(ac.get("ownOp", "")).strip().upper()
    flight = str(ac.get("flight", "")).strip().upper()
    ac_type = str(ac.get("t", "")).strip().upper().replace(" ", "")
    desc = str(ac.get("desc", "")).strip().upper()

    if flight.startswith("RCH") or flight.startswith("REACH"):
        return False

    if tail == PRIORITY_TAIL:
        return True

    is_ncr_callsign = flight.startswith("NCR")
    is_national_op = any(op in own_op for op in NATIONAL_OPERATORS)
    
    is_target_type = False
    for target in NATIONAL_TARGET_TYPES:
        target_clean = target.replace("-", "")
        if (target == ac_type or target_clean == ac_type or
            target in desc or target_clean in desc):
            is_target_type = True
            break

    if (is_ncr_callsign or is_national_op) and is_target_type:
        return True

    if is_ncr_callsign:
        return True

    return False


# ==========================================
# 通知送信関数 (米軍・オメガ機用)
# ==========================================
def send_military_notification(icao, tail, flight, ac_type, own_op, alt, track, origin, destination, location_str, event_type="検知"):
    flight_str = flight if flight else "不明"
    tail_str = tail if tail else "不明"
    type_str = ac_type if ac_type else "対象機"
    op_str = own_op if own_op else "米軍/関連機関"
    direction_str = get_direction_text(track)
    is_japan_airport = is_destination_japan_airport(destination)

    # カラー切り分け
    if is_japan_airport:
        embed_color = 0xFF0000
        content_text = f"🚨 **【重要】{type_str} ({tail_str}) の目的地が「日本の空港 ({destination})」に設定されました！** @everyone"
    else:
        type_clean = type_str.lower().replace(" ", "").replace("-", "")
        if any(k in type_clean for k in ["w135", "wc135", "r135", "rc135", "oc135", "ec135"]):
            embed_color = 0x9B59B6
        elif any(k in type_clean for k in ["e3", "sentry"]):
            embed_color = 0xF1C40F
        elif any(k in type_clean for k in ["k35", "kc135", "c135", "kdc10", "dc10"]):
            embed_color = 0xE67E22
        elif any(k in type_clean for k in ["e4", "vc25", "e6", "e8"]):
            embed_color = 0x900C3F
        else:
            embed_color = 0x3498DB
        content_text = f"✈️ **【米軍機{event_type}】{type_str}: {tail_str} (Callsign: {flight_str})**"

    payload = {
        "content": content_text,
        "embeds": [
            {
                "title": f"✈️ {type_str} 飛行ステータス詳細 ({event_type})",
                "color": embed_color,
                "fields": [
                    {"name": "機体型式 (Type)", "value": type_str, "inline": True},
                    {"name": "所属/運用者 (Operator)", "value": op_str, "inline": True},
                    {"name": "機体番号 (Tail / Reg)", "value": tail_str, "inline": True},
                    {"name": "フライト番号 (Callsign)", "value": flight_str, "inline": True},
                    {"name": "ICAOコード", "value": icao.upper(), "inline": True},
                    {"name": "高度", "value": f"{alt} ft" if isinstance(alt, (int, float)) else str(alt), "inline": True},
                    {"name": "🧭 進行方位（向き）", "value": direction_str, "inline": True},
                    {"name": "📍 反応位置（地名＆座標）", "value": location_str, "inline": False},
                    {"name": "🛫 出発地", "value": origin, "inline": True},
                    {"name": "🛬 目的地", "value": destination, "inline": True},
                ],
                "footer": {"text": "ADSB Military Tracker"}
            }
        ]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[{time.strftime('%H:%M:%S')}] 米軍Discord通知完了({event_type}): {type_str} {tail_str} ({flight_str})")
    except Exception as e:
        print(f"米軍送信エラー: {e}")


# ==========================================
# 通知送信関数 (National Airlines用)
# ==========================================
def send_national_notification(icao, tail, flight, ac_type, own_op, alt, track, origin, destination, location_str, event_type="検知"):
    flight_str = flight if flight else "不明"
    tail_str = tail if tail else "不明"
    type_str = ac_type if ac_type else "National Airlines 機"
    op_str = own_op if own_op else "National Airlines"
    direction_str = get_direction_text(track)
    
    is_priority_aircraft = (tail_str.upper() == PRIORITY_TAIL)
    is_dest_japan = is_destination_japan_airport(destination)
    is_origin_japan = is_destination_japan_airport(origin)
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
        print(f"[{time.strftime('%H:%M:%S')}] National Discord通知完了({event_type}): {type_str} {tail_str} ({flight_str})")
    except Exception as e:
        print(f"National送信エラー: {e}")


# ==========================================
# メインの定期監視ループ処理
# ==========================================
def run_dual_tracking():
    global mil_in_air_states, mil_notified_icaos
    global nat_in_air_states, nat_notified_icaos

    url = "https://api.adsb.lol/v2/mil"
    try:
        res = requests.get(url, timeout=15).json()
        ac_list = res.get("ac", [])
        if not ac_list:
            return

        mil_current_batch = set()
        nat_current_batch = set()

        for ac in ac_list:
            icao = ac.get("hex", "").strip()
            if not icao:
                continue

            tail = ac.get("r", "N/A").strip()
            flight = ac.get("flight", "N/A").strip()
            ac_type = ac.get("t", ac.get("desc", "不明")).strip()
            own_op = ac.get("ownOp", "不明").strip()
            alt = ac.get("alt_baro")
            track = ac.get("track")
            lat = ac.get("lat")
            lon = ac.get("lon")

            is_ground = (alt == "ground") or (isinstance(alt, (int, float)) and alt < 100)
            is_in_air_current = not is_ground

            # --- 1. 米軍・オメガ機のチェック ---
            if is_target_military_aircraft(ac):
                mil_current_batch.add(icao)
                mil_last = mil_in_air_states.get(icao)

                is_takeoff = (mil_last is False and is_in_air_current is True)
                is_new = (icao not in mil_notified_icaos and is_in_air_current)

                if is_takeoff or is_new:
                    event_type = "離陸" if is_takeoff else "検知"
                    print(f"【米軍機 {event_type】 機種: {ac_type}, Tail: {tail}, Flight: {flight}")
                    location_str = get_location_name(lat, lon)
                    fa_origin, destination = get_flight_route(flight)
                    origin = fa_origin if fa_origin != "不明" else location_str
                    send_military_notification(icao, tail, flight, ac_type, own_op, alt, track, origin, destination, location_str, event_type)
                    mil_notified_icaos.add(icao)

                mil_in_air_states[icao] = is_in_air_current

            # --- 2. National Airlines のチェック ---
            if is_national_airlines(ac):
                nat_current_batch.add(icao)
                nat_last = nat_in_air_states.get(icao)

                is_takeoff = (nat_last is False and is_in_air_current is True)
                is_new = (icao not in nat_notified_icaos and is_in_air_current)

                if is_takeoff or is_new:
                    event_type = "離陸" if is_takeoff else "検知"
                    print(f"【National {event_type}】 機種: {ac_type}, Tail: {tail}, Flight: {flight}")
                    location_str = get_location_name(lat, lon)
                    fa_origin, destination = get_flight_route(flight)
                    origin = fa_origin if fa_origin != "不明" else location_str
                    send_national_notification(icao, tail, flight, ac_type, "National Airlines", alt, track, origin, destination, location_str, event_type)
                    nat_notified_icaos.add(icao)

                nat_in_air_states[icao] = is_in_air_current

        # 圏外に出た機体をクリア
        mil_notified_icaos = mil_notified_icaos.intersection(mil_current_batch)
        nat_notified_icaos = nat_notified_icaos.intersection(nat_current_batch)

    except Exception as e:
        print(f"統合チェック中エラー: {e}")


if __name__ == "__main__":
    print("米軍機・オメガ機 ＆ National Airlines のデュアル監視システムを開始しました...")
    
    # 起動時の即時テスト通知
    send_startup_notification()
    
    # 監視ループ
    while True:
        time.sleep(CHECK_INTERVAL)
        run_dual_tracking()
