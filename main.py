import os
import time
import math
import threading
from datetime import datetime, timezone, timedelta
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

# 1. 許可する指定運用者（National除外、純米軍＋オメガ空中給油機）
ALLOWED_OPERATORS = [
    "us air force", "usaf", "united states air force",
    "us navy", "usn", "united states navy",
    "us marine corps", "usmc", "united states marine corps",
    "omega air", "omega aerial refueling", "omega tanker"
]

# 2. 米軍特有の代表的コールサイン
TARGET_CALLSIGNS = [
    "sentry",    # E-3 AWACS
    "recon", "jake", "cobra", "snoop", "bolt", "pyton", "olay", # RC-135 / WC-135 / OC-135
    "hobo", "gold", "ethyl", "teal", "qid", "m35", "boeing" # Tanker / C-135系 / USAF
]

# 3. 監視対象の指定機種（徹底網羅）
TARGET_TYPES = [
    # C-135 派生
    "k35r", "k35q", "c135", "c35", "kc135", "kc-135", "nc135", "tc135",
    "r135", "rc135", "rc-135",
    "w135", "wc135", "wc-135", "wc135w", "wc135c",
    "ec135", "ec-135", "oc135", "oc-135",
    
    # E-3 (AWACS) 関連
    "e3tf", "e3cf", "e3a", "e3b", "e3c", "e3d", "e3g", "e3", "e-3", "sentry",
    
    # E-4 / VC-25 / E-6 / E-8 / B707派生
    "e4", "e4b", "e-4b", "vc25", "vc25a", "vc-25a", "vc25b", "vc-25b",
    "b707", "b-707", "b703", "b-703", "boeing707", "boeing 707",
    "e6", "e-6", "e6b", "e-6b", "e8", "e-8", "e8c", "e-8-c", "c137", "c-137",

    # KDC-10 / DC-10 タンク機
    "kdc10", "kdc-10", "dc10", "dc-10", "dc103"
]

# 4. 明確に除外したい機種
EXCLUDE_TYPES = [
    "e390", "e-390", "kc390", "kc-390", "c390", "c-390",
    "h60", "h-60", "mh60", "mh-60", "sh60", "sh-60", "hh60", "hh-60", "uh60", "uh-60",
    "blackhawk", "seahawk", "jayhawk", "knighthawk"
]

# 5. 世界中の主要な米軍基地・重要拠点および主要国際空港の3レターコード辞書
AIRPORT_CODE_DICT = {
    # --- 日本国内の米軍基地・自衛隊基地 ---
    "横田": "OKO (RJTY)", "yokota": "OKO (RJTY)",
    "厚木": "NJA (RJTA)", "atsugi": "NJA (RJTA)",
    "嘉手納": "DNA (RODN)", "kadena": "DNA (RODN)",
    "普天間": "ODT (ROTM)", "futenma": "ROTM",
    "三沢": "MSJ (RJSM)", "misawa": "MSJ (RJSM)",
    "岩国": "IWK (RJOI)", "iwakuni": "RJOI",
    "横須賀": "YOKOSUKA", "佐世保": "SASEBO", "座間": "ZAMA",
    "那覇": "OKA (ROAH)", "naha": "OKA (ROAH)",
    "羽田": "HND (RJTT)", "成田": "NRT (RJAA)", "関西": "KIX (RJBB)",
    "中部": "NGO (RJGG)", "セントレア": "NGO (RJGG)",
    "伊丹": "ITM (RJOO)", "福岡": "FUK (RJFF)", "新千歳": "CTS (RJCC)",
    "小松": "KMQ (RJNK)", "百里": "IBR (RJAH)", "茨城": "IBR (RJAH)",
    "浜松": "HMT (RJNH)", "美保": "YGJ (RJOH)", "築城": "TKJ (RJFZ)",
    "新田原": "NVR (RJFN)", "小牧": "NKM (RJNA)",

    # --- グアム・ハワイ・太平洋地域 ---
    "アンダーセン": "UAM (PGUA)", "andersen": "UAM (PGUA)",
    "グアム": "GUM (PGUM)", "guam": "GUM (PGUM)",
    "ヒッカム": "HIK (PHIK)", "hickam": "HIK (PHIK)",
    "ホノルル": "HNL (PHNL)", "honolulu": "HNL (PHNL)",
    "真珠湾": "PHCF", "pearl harbor": "PHCF",
    "ウェーク島": "AWK (PWAK)", "wake island": "PWAK",
    "ミッドウェー": "MDY (PMDY)",
    "ジョンストン": "JON",

    # --- 韓国の米軍基地・主要空港 ---
    "オスサン": "OSN (RKSO)", "osan": "OSN (RKSO)",
    "群山": "KUV (RKJK)", "kunsan": "KUV (RKJK)",
    "仁川": "ICN (RKSI)", "incheon": "ICN (RKSI)",
    "金浦": "GMP (RKSS)", "釜山": "PUS (RKPK)",

    # --- アラスカ・アメリカ本土の主要空軍基地（USAF / 米軍） ---
    "エルメンドルフ": "EDF (PAEI)", "elmendorf": "EDF (PAEI)",
    "アイェルソン": "FAI (PAFA)", "eielson": "EIL (PAEI)",
    "ネリス": "LSV (KLSV)", "nellis": "LSV (KLSV)",
    "ライトパターソン": "FFO (KFFO)", "wright-patterson": "FFO (KFFO)",
    "トラビス": "SUU (KSUU)", "travis": "SUU (KSUU)",
    "ドーバー": "DOV (KDOV)", "dover": "KDOV",
    "マクコード": "TCM (KTCM)", "mcchord": "KTCM",
    "マクディル": "MCF (KMCF)", "macdill": "KMCF",
    "シャールズ": "CHS (KCHS)", "charleston": "KCHS",
    "スコット": "BLV (KBLV)", "scott": "KBLV",
    "キャノン": "CVS (KCVS)", "cannon": "KCVS",
    "ホロマン": "HMN (KHMN)", "holloman": "KHMN",
    "ダイエス": "DYS (KDYS)", "dyess": "KDYS",
    "エルスワース": "RMP (KRMP)", "ellsworth": "KRMP",
    "グランドフォークス": "RDR (KRDR)", "grand forks": "KRDR",
    "マッコーネル": "IAB (KIAB)", "mcconnell": "KIAB",
    "フェアートチャイルド": "SKA (KSKA)", "fairchild": "KSKA",
    "キース": "MGE (KMGE)", "marietta": "KMGE",
    "バルティモア": "BWI (KBWI)",
    "ワシントン": "IAD (KIAD)", "andrews": "ADW (KADW)", "アンドルーズ": "ADW (KADW)",

    # --- ヨーロッパ・中東・その他の主要基地 ---
    "ラムシュタイン": "RMS (ETAR)", "ramstein": "RMS (ETAR)",
    "ラメンホーフ": "RMS (ETAR)",
    "レイクheath": "LKH (EGUL)", "lakenheath": "LKH (EGUL)",
    "ミルデンホール": "MHZ (EGUN)", "mildenhall": "MHZ (EGUN)",
    "シモネラ": "Aviano", "aviano": "AVO (LIPA)",
    "インジルリク": "INC (LTAG)", "incirlik": "INC (LTAG)",
    "アルウデイド": "XJD (OTBH)", "al udeid": "XJD (OTBH)",
    "ダフラ": "DHA (OMAM)", "al dhafra": "DHA (OMAM)"
}

if not DISCORD_WEBHOOK_URL:
    raise ValueError("エラー: DISCORD_WEBHOOK_URL が設定されていません。")

in_air_states = {}
notified_icaos = set()


def get_jst_now_str():
    """現在の日本時間（JST）を文字列で取得する"""
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime('%Y-%m-%d %H:%M:%S (JST)')


def send_startup_notification():
    payload = {
        "content": "🚀 **【システム起動成功】世界対応・3レターコード監視プログラムがLive化しました！**",
        "embeds": [{
            "title": "🚀 起動・接続テスト",
            "color": 0x2ECC71,
            "fields": [
                {"name": "ステータス", "value": "世界中の米軍基地・国際空港3レター対応型 稼働中", "inline": True},
                {"name": "更新間隔", "value": f"{CHECK_INTERVAL}秒", "inline": True},
                {"name": "時刻", "value": get_jst_now_str(), "inline": True},
            ],
            "footer": {"text": "ADSB Military Tracker - Global 3-Letter"}
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[{get_jst_now_str()}] 起動直後テスト通知の送信成功！")
    except Exception as e:
        print(f"起動テスト送信エラー: {e}")


def get_embed_color(ac_type, is_japan_destination):
    if is_japan_destination:
        return 0xFF0000  # 赤色（日本目的地・最高警戒）

    type_clean = ac_type.lower().replace(" ", "").replace("-", "")

    if any(k in type_clean for k in ["w135", "wc135", "r135", "rc135", "oc135", "ec135"]):
        return 0x9B59B6  # 紫色（偵察機）

    if any(k in type_clean for k in ["e3", "sentry"]):
        return 0xF1C40F  # 黄色（AWACS）

    if any(k in type_clean for k in ["e4", "vc25", "e6", "e8"]):
        return 0x900C3F  # 濃い赤（指揮統制機）

    if any(k in type_clean for k in ["k35", "kc135", "c135", "kdc10", "dc10"]):
        return 0xE67E22  # オレンジ色（タンカー）

    return 0x3498DB  # 青色


def is_target_aircraft(ac):
    lat = ac.get("lat")
    lon = ac.get("lon")
    alt = ac.get("alt_baro")
    if lat is None or lon is None or alt is None:
        return False

    ac_type = str(ac.get("t", "")).strip().lower().replace(" ", "")
    desc = str(ac.get("desc", "")).strip().lower()
    own_op = str(ac.get("ownOp", "")).strip().lower()
    flight = str(ac.get("flight", "")).strip().lower()

    if "national air" in own_op or flight.startswith("ncr") or flight.startswith("rch") or flight.startswith("reach"):
        return False

    for exclude in EXCLUDE_TYPES:
        exclude_clean = exclude.replace("-", "")
        if (exclude in ac_type or exclude_clean in ac_type or
            exclude in desc or exclude_clean in desc):
            return False

    is_target_callsign = any(cs in flight for cs in TARGET_CALLSIGNS)

    is_allowed_op = False
    if own_op:
        is_allowed_op = any(op in own_op for op in ALLOWED_OPERATORS)
    else:
        is_allowed_op = True

    desc_clean = desc.replace(" ", "").replace("-", "")
    is_target_type = False

    for target in TARGET_TYPES:
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


def find_airport_code(text):
    """取得したテキスト内から世界中の辞書に合致する基地・空港を探して3レターコード付きの文字列を返す"""
    if not text:
        return None
    text_lower = text.lower()
    for key, code in AIRPORT_CODE_DICT.items():
        if key in text or key.lower() in text_lower:
            return f"[{code}] {text}"
    return None


def get_location_name(lat, lon):
    """座標から世界中の最寄り空港・基地名や3レターコード付きの地名を取得する"""
    if lat is None or lon is None:
        return "位置情報なし"
    
    coord_str = f"({round(lat, 4)}, {round(lon, 4)})"
    
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=11"
        headers = {"User-Agent": "ADSB-Military-Tracker/2.2"}
        res = requests.get(url, headers=headers, timeout=5).json()
        
        address = res.get("address", {})
        
        aerodrome = address.get("aerodrome")
        military = address.get("military")
        airway = address.get("aeroway")
        city = (address.get("city") or 
                address.get("town") or 
                address.get("village") or 
                address.get("county") or 
                address.get("state") or "")
        
        facility_name = aerodrome or military or airway
        
        # 1. 施設名（飛行場や基地）が取れた場合
        if facility_name:
            matched_with_code = find_airport_code(facility_name)
            if matched_with_code:
                return f"🛬 **{matched_with_code}** 付近 {coord_str}"
            return f"🛬 **{facility_name}** 付近 ({city}) {coord_str}"
        
        # 2. 市区町村名などから基地・空港の名前が含まれていないかチェック
        matched_with_code_city = find_airport_code(city)
        if matched_with_code_city:
            return f"🛬 **{matched_with_code_city}** 上空/周辺 {coord_str}"
        
        # 3. 通常の都市名
        if city:
            return f"📍 **{city}** 上空/周辺 {coord_str}"
        else:
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


def send_discord_notification(icao, tail, flight, ac_type, own_op, alt, track, origin, destination, location_str, event_type="検知"):
    flight_str = flight if flight else "不明"
    tail_str = tail if tail else "不明"
    type_str = ac_type if ac_type else "対象機"
    op_str = own_op if own_op else "米軍/関連機関"
    direction_str = get_direction_text(track)
    is_japan_airport = is_destination_japan_airport(destination)
    detection_time_str = get_jst_now_str()

    embed_color = get_embed_color(type_str, is_japan_airport)

    if is_japan_airport:
        content_text = f"🚨 **【重要】{type_str} ({tail_str}) の目的地が「日本の空港 ({destination})」に設定されました！** @everyone"
    else:
        content_text = f"✈️ **【米軍機{event_type}】{type_str}: {tail_str} (Callsign: {flight_str})**"

    payload = {
        "content": content_text,
        "embeds": [
            {
                "title": f"✈️ {type_str} 飛行ステータス詳細 ({event_type})",
                "color": embed_color,
                "fields": [
                    {"name": "🕒 通過時刻 (日本時間)", "value": detection_time_str, "inline": False},
                    {"name": "🛫 最寄り空港・通過位置", "value": location_str, "inline": False},
                    {"name": "機体型式 (Type)", "value": type_str, "inline": True},
                    {"name": "所属/運用者 (Operator)", "value": op_str, "inline": True},
                    {"name": "機体番号 (Tail / Reg)", "value": tail_str, "inline": True},
                    {"name": "フライト番号 (Callsign)", "value": flight_str, "inline": True},
                    {"name": "ICAOコード", "value": icao.upper(), "inline": True},
                    {"name": "高度", "value": f"{alt} ft" if isinstance(alt, (int, float)) else str(alt), "inline": True},
                    {"name": "🧭 進行方位（向き）", "value": direction_str, "inline": True},
                    {"name": "🛫 出発地", "value": origin, "inline": True},
                    {"name": "🛬 目的地", "value": destination, "inline": True},
                ],
                "footer": {"text": "ADSB Military Tracker - Global 3-Letter"}
            }
        ]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"[{get_jst_now_str()}] Discord通知完了({event_type}): {type_str} {tail_str} ({flight_str})")
    except Exception as e:
        print(f"送信エラー: {e}")


def check_military_takeoff():
    global in_air_states, notified_icaos

    url = "https://api.adsb.lol/v2/mil"
    try:
        res = requests.get(url, timeout=15).json()
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
            is_in_air_last = in_air_states.get(icao)

            is_takeoff = (is_in_air_last is False and is_in_air_current is True)
            is_new_detection = (icao not in notified_icaos and is_in_air_current)

            if is_takeoff or is_new_detection:
                event_type = "離陸" if is_takeoff else "検知"
                print(f"【{event_type}】 機種: {ac_type}, Tail: {tail}, Flight: {flight}")
                
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
    print("米軍機・特殊機（世界対応・3レターコード版）システムを開始しました...")
    
    send_startup_notification()
    
    while True:
        time.sleep(CHECK_INTERVAL)
        check_military_takeoff()
