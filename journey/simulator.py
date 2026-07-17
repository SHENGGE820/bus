#!/usr/bin/env python3
"""
通勤模擬器 - 即時多段追蹤
機場捷運 + 環狀線 + 公車 262
用法：python simulator.py
然後在手機瀏覽器開 http://電腦IP:8080
"""

import threading
import time
import datetime
import json
import os
import sys
import socket
import requests
from flask import Flask, jsonify, request, send_from_directory

# ── 路徑 ────────────────────────────────────────────────────
DIR         = os.path.dirname(os.path.abspath(__file__))
JOURNEY_FILE = os.path.join(DIR, "journey.json")
CONFIG_FILE  = os.path.join(DIR, "..", "config.json")

# ── TDX ─────────────────────────────────────────────────────
TDX_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_BASE      = "https://tdx.transportdata.tw/api/basic"

_token        = None
_token_expiry = None


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if os.environ.get("TDX_CLIENT_ID"):
        cfg["client_id"] = os.environ["TDX_CLIENT_ID"]
    if os.environ.get("TDX_CLIENT_SECRET"):
        cfg["client_secret"] = os.environ["TDX_CLIENT_SECRET"]
    return cfg


def get_token(cfg):
    global _token, _token_expiry
    now = datetime.datetime.now()
    if _token and _token_expiry and now < _token_expiry:
        return _token
    r = requests.post(TDX_TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }, timeout=10)
    r.raise_for_status()
    d = r.json()
    _token = d["access_token"]
    _token_expiry = now + datetime.timedelta(seconds=d.get("expires_in", 1800) - 60)
    return _token


def tdx(path, cfg, params=None):
    token = get_token(cfg)
    p = {"$format": "JSON"}
    if params:
        p.update(params)
    url = f"{TDX_BASE}/{path}"
    headers = {"Authorization": "Bearer " + token}
    r = requests.get(url, headers=headers, params=p, timeout=10)
    if r.status_code == 429:
        try:
            retry_after = min(5.0, max(1.0, float(r.headers.get("Retry-After", "2"))))
        except ValueError:
            retry_after = 2.0
        time.sleep(retry_after)
        r = requests.get(url, headers=headers, params=p, timeout=10)
    r.raise_for_status()
    return r.json()


# ── TDX 查詢 ─────────────────────────────────────────────────

def get_metro_next(system, station_id, headsign_kw, cfg, line_id=None):
    """
    取得某捷運站下一班往指定方向的列車。
    回傳 EstimateTime（整數分鐘），找不到回傳 None。
    headsign_kw: TripHeadSign 裡要包含的關鍵字（例如「台北車站」「大坪林」）
    line_id: 環狀線需額外傳 'Y' 才能正確過濾
    """
    f = f"StationID eq '{station_id}'"
    if line_id:
        f += f" and LineID eq '{line_id}'"
    data = tdx(f"v2/Rail/Metro/LiveBoard/{system}", cfg, {"$filter": f})
    # 僅接受指定方向；查不到時不可退回任意方向，否則會自動上錯車。
    trains = [
        train for train in data
        if headsign_kw in train.get("TripHeadSign", "")
        or headsign_kw in (train.get("DestinationStationName") or {}).get("Zh_tw", "")
    ]
    trains = [train for train in trains if isinstance(train.get("EstimateTime"), int)]
    if not trains:
        return None
    return min(train["EstimateTime"] for train in trains)


def get_bus_eta(city, route, stop_name, direction, cfg):
    """取得公車下一班到站預估秒數"""
    data = tdx(
        f"v2/Bus/EstimatedTimeOfArrival/City/{city}/{route}",
        cfg,
        {
            "$filter":  f"StopName/Zh_tw eq '{stop_name}' and Direction eq {direction}",
            "$orderby": "EstimateTime asc"
        }
    )
    estimates = [item for item in data if isinstance(item.get("EstimateTime"), int)]
    if estimates:
        next_bus = min(estimates, key=lambda item: item["EstimateTime"])
        est = next_bus["EstimateTime"]
        plate = next_bus.get("PlateNumb") or "?"
        return est, plate
    return None, None


def get_bus_near_stop(city, route, direction, cfg):
    """取得公車各站即時位置"""
    return tdx(
        f"v2/Bus/RealTimeNearStop/City/{city}/{route}",
        cfg,
        {"$filter": f"Direction eq {direction}"}
    )


# ── 地圖座標 ─────────────────────────────────────────────────
_waypoints = []   # [{id, lat, lng, name}, ...]  由背景任務填入


def get_bus_live_pos(city, route, direction, plate, cfg):
    """查公車即時 GPS（PlateNumb 精確比對）"""
    try:
        d = tdx(f"v2/Bus/RealTimeByRoute/City/{city}/{route}", cfg,
                {"$filter": f"Direction eq {direction}"})
        for b in d:
            if b.get("PlateNumb", "") == plate:
                pos = b.get("BusPosition", {})
                lat = pos.get("PositionLat")
                lng = pos.get("PositionLon")
                if lat and lng:
                    return lat, lng
    except:
        pass
    return None, None


def _fetch_waypoints_task():
    """背景任務：啟動時從 TDX 取得路線各站/站牌座標"""
    global _waypoints
    time.sleep(1)   # 等 token 就緒
    coords = {}

    try:  # 機場捷運站
        for s in tdx("v2/Rail/Metro/Station/TYMC", CFG):
            sid = s.get("StationID", "")
            p   = s.get("StationPosition", {})
            name = s.get("StationName", {}).get("Zh_tw", sid)
            coords[sid] = (p.get("PositionLat"), p.get("PositionLon"), name)
    except Exception as e:
        print(f"[map] TYMC 失敗: {e}")

    try:  # 環狀線
        for s in tdx("v2/Rail/Metro/Station/TRTC", CFG,
                     {"$filter": "LineID eq 'Y'"}):
            sid = s.get("StationID", "")
            p   = s.get("StationPosition", {})
            name = s.get("StationName", {}).get("Zh_tw", sid)
            coords[sid] = (p.get("PositionLat"), p.get("PositionLon"), name)
    except Exception as e:
        print(f"[map] TRTC 失敗: {e}")

    try:  # 262 公車站牌
        d = tdx("v2/Bus/StopOfRoute/City/NewTaipei/262", CFG,
                {"$filter": "Direction eq 0"})
        stops = d[0].get("Stops", []) if d else []
        for stop in stops:
            name = stop.get("StopName", {}).get("Zh_tw", "")
            p    = stop.get("StopPosition", {})
            if name:
                coords[f"stop:{name}"] = (p.get("PositionLat"), p.get("PositionLon"), name)
    except Exception as e:
        print(f"[map] Bus 262 站牌失敗: {e}")

    # start / end 從 journey.json 讀
    jdata = json.load(open(JOURNEY_FILE, encoding="utf-8"))
    sc = jdata.get("start_coord", [25.044, 121.422])
    ec = jdata.get("end_coord",   [24.988, 121.483])
    coords["__start"] = (sc[0], sc[1], "出發點")
    coords["__end"]   = (ec[0], ec[1], "目的地")

    order = ["__start", "A4", "A3", "Y13",
             "stop:捷運橋和站", "stop:臺灣新北地方法院(金城)", "__end"]
    wpts = [{"id": k, "lat": coords[k][0], "lng": coords[k][1], "name": coords[k][2]}
            for k in order if k in coords and coords[k][0]]

    with _lock:
        _waypoints = wpts

    print(f"[map] {len(wpts)} 個路線點：")
    for w in wpts:
        print(f"  {w['id']:42} {w['lat']:.5f}, {w['lng']:.5f}  {w['name']}")


# ── 狀態機 ───────────────────────────────────────────────────

JOURNEY   = None   # 從 journey.json 讀入
CFG       = None
STATUS    = {
    "running": False,
    "current_leg": -1,
    "legs": [],
    "height_cm": 170.0,
    "started_at": None,
    "eta_str": "--:--",
    "updated_at": "",
    "error": ""
}
_lock = threading.RLock()


def now_str():
    return datetime.datetime.now().strftime("%H:%M:%S")


def ts():
    return datetime.datetime.now()


def eta_from_now(remaining_min):
    t = datetime.datetime.now() + datetime.timedelta(minutes=remaining_min)
    return t.strftime("%H:%M")


def apply_height_to_walk_legs(height_cm):
    """以 170 cm 為原始步行時間基準，依步幅近似比例調整。"""
    factor = 170.0 / height_cm
    with _lock:
        STATUS["height_cm"] = height_cm
        for leg in STATUS["legs"]:
            if leg["type"] == "walk":
                leg["est_min"] = round(leg["base_est_min"] * factor, 1)


def recalculate_eta():
    total_remain = 0.0
    with _lock:
        for i, leg in enumerate(STATUS["legs"]):
            s = leg["status"]
            if s == "completed":
                continue
            elif s in ("walking", "waiting", "on_board"):
                elapsed = (ts() - datetime.datetime.fromisoformat(
                    leg.get("started_at", ts().isoformat()))).total_seconds() / 60
                remain = max(0, leg["est_min"] - elapsed)
                total_remain += remain
                # add future legs
                for j in range(i + 1, len(STATUS["legs"])):
                    future = STATUS["legs"][j]
                    total_remain += future["est_min"]
                    if future["type"] in ("metro", "bus"):
                        total_remain += 5  # 平均等車時間
                break
            elif s == "pending":
                total_remain += leg["est_min"]
                if leg["type"] in ("metro", "bus"):
                    total_remain += 5
        STATUS["eta_str"] = eta_from_now(total_remain)
        STATUS["updated_at"] = now_str()


def advance_to_next_leg():
    with _lock:
        cur = STATUS["current_leg"]
        if cur >= 0:
            STATUS["legs"][cur]["status"] = "completed"
            STATUS["legs"][cur]["ended_at"] = now_str()
        nxt = cur + 1
        if nxt >= len(STATUS["legs"]):
            STATUS["running"] = False
            STATUS["current_leg"] = -1
            return
        STATUS["current_leg"] = nxt
        leg = STATUS["legs"][nxt]
        leg["started_at"] = ts().isoformat()
        if leg["type"] == "walk":
            leg["status"] = "walking"
        else:
            leg["status"] = "waiting"
        leg["info"] = ""
        leg["vehicle_id"] = ""
        leg["current_stop"] = ""


def user_board():
    """使用者按下「上車」"""
    with _lock:
        cur = STATUS["current_leg"]
        if cur < 0:
            return False
        leg = STATUS["legs"][cur]
        if leg["status"] == "waiting":
            leg["status"] = "on_board"
            leg["boarded_at"] = now_str()
            leg["started_at"] = ts().isoformat()  # 重設計時起點為上車時刻
            return True
    return False


def auto_board_leg(leg, vehicle_id=""):
    """TDX 偵測車輛進站後，自動把目前行程切換為已上車。"""
    with _lock:
        cur = STATUS["current_leg"]
        if cur < 0 or STATUS["legs"][cur] is not leg or leg["status"] != "waiting":
            return False
        leg["status"] = "on_board"
        leg["boarded_at"] = now_str()
        leg["started_at"] = ts().isoformat()
        if vehicle_id:
            leg["vehicle_id"] = vehicle_id
        leg["info"] = "已偵測進站，自動上車"
        return True


# ── 步行腿更新 ───────────────────────────────────────────────

def update_walk_leg(leg):
    elapsed = (ts() - datetime.datetime.fromisoformat(leg["started_at"])).total_seconds() / 60
    remain  = max(0, leg["est_min"] - elapsed)
    leg["info"] = f"剩餘約 {remain:.0f} 分鐘"
    if elapsed >= leg["est_min"]:
        advance_to_next_leg()


# ── 捷運腿更新 ───────────────────────────────────────────────

def update_metro_waiting(leg):
    """等車中：顯示下一班列車（EstimateTime 為整數分鐘）"""
    try:
        est = get_metro_next(
            leg["system"], leg["from_id"],
            leg.get("headsign", ""), CFG,
            line_id=leg.get("line_id")
        )
        headsign = leg.get("headsign", "")
        if est is None:
            leg["info"] = "查詢中…"
        elif est == 0:
            auto_board_leg(leg)
        else:
            leg["info"] = f"下一班（往{headsign}）：{est} 分後到站"
    except Exception as e:
        leg["info"] = f"⚠ {e}"


def update_metro_on_board(leg):
    """
    在車上：持續查目的站 LiveBoard。
    TDX 捷運 LiveBoard 沒有 TrainNo，因此用「方向 + 上車時間 +
    目的站到站事件」配對，並設最短行駛時間避免誤認前一班列車。
    """
    elapsed = (ts() - datetime.datetime.fromisoformat(leg["started_at"])).total_seconds() / 60
    remain  = max(0, leg["est_min"] - elapsed)
    min_arrival_minutes = max(0.5, leg["est_min"] * 0.35)

    # 查目的站；EstimateTime == 0 代表相同方向列車已抵達目的站。
    try:
        est_dest = get_metro_next(
            leg["system"], leg["to_id"],
            leg.get("headsign", ""), CFG,
            line_id=leg.get("line_id")
        )
        if est_dest == 0 and elapsed >= min_arrival_minutes:
            leg["info"] = f"已抵達 {leg['to_name']}，自動下車"
            advance_to_next_leg()
            return
        if est_dest is not None:
            leg["info"] = f"行駛中　目的站 {leg['to_name']}：{est_dest} 分後有車抵達"
        else:
            leg["info"] = f"行駛中　約 {remain:.0f} 分鐘到 {leg['to_name']}"
    except Exception:
        leg["info"] = f"行駛中　約 {remain:.0f} 分鐘到 {leg['to_name']}"

    # TDX 若漏掉到站事件，以預估時間 + 1 分鐘作為安全備援。
    if elapsed >= leg["est_min"] + 1:
        leg["info"] = f"已到達 {leg['to_name']}（預估時間備援）"
        advance_to_next_leg()


# ── 公車腿更新 ───────────────────────────────────────────────

def update_bus_waiting(leg):
    """等車中：顯示下一班公車資訊"""
    try:
        est, plate = get_bus_eta(
            leg["city"], leg["route"], leg["from_stop"], leg["direction"], CFG)
        if est is not None and est >= 0:
            leg["info"] = f"下一班 {plate}：{est // 60} 分後到站"
            leg["vehicle_id"] = plate
        else:
            leg["info"] = "查詢中…"
    except Exception as e:
        leg["info"] = f"ETA 查詢中（{e}）"

    # ETA 只代表預估；以 RealTimeNearStop 的進站事件作為自動上車依據。
    try:
        near = get_bus_near_stop(
            leg["city"], leg["route"], leg["direction"], CFG)
        arrivals = [
            bus for bus in near
            if bus.get("StopName", {}).get("Zh_tw", "") == leg["from_stop"]
            and bus.get("A2EventType") in (0, 1)
            and bus.get("PlateNumb", "")
        ]
        if arrivals:
            expected_plate = leg.get("vehicle_id", "")
            arriving = next(
                (bus for bus in arrivals if bus.get("PlateNumb") == expected_plate),
                arrivals[0]
            )
            auto_board_leg(leg, arriving.get("PlateNumb", ""))
    except Exception as e:
        if not leg.get("info"):
            leg["info"] = f"進站偵測中（{e}）"


def update_bus_on_board(leg):
    """在車上：追蹤公車到目的站，並取得即時 GPS 位置"""
    plate = leg.get("vehicle_id", "")

    # ── GPS 即時位置 ──────────────────────────────────────────
    if plate:
        lat, lng = get_bus_live_pos(
            leg["city"], leg["route"], leg["direction"], plate, CFG)
        if lat:
            leg["lat"] = lat
            leg["lng"] = lng

    try:
        near = get_bus_near_stop(leg["city"], leg["route"], leg["direction"], CFG)

        # 更新目前站
        for bus in near:
            if bus.get("PlateNumb", "") == plate:
                stop = bus.get("StopName", {}).get("Zh_tw", "")
                if stop:
                    leg["current_stop"] = stop

        # 判斷是否到達下車站
        at_dest = [b for b in near
                   if b.get("StopName", {}).get("Zh_tw", "") == leg["to_stop"]
                   and b.get("A2EventType") in (0, 1)
                   and b.get("PlateNumb", "") == plate]
        if at_dest:
            leg["info"] = f"已抵達 {leg['to_stop']}，自動下車"
            advance_to_next_leg()
            return

        elapsed = (ts() - datetime.datetime.fromisoformat(leg["started_at"])).total_seconds() / 60
        remain  = max(0, leg["est_min"] - elapsed)
        cur_stop = leg.get("current_stop", "")
        leg["info"] = f"{'📍 ' + cur_stop if cur_stop else '行駛中'}　剩約 {remain:.0f} 分"

        # TDX 若漏掉目的站事件，以預估時間 + 2 分鐘作為安全備援。
        if elapsed >= leg["est_min"] + 2:
            advance_to_next_leg()
    except Exception as e:
        elapsed = (ts() - datetime.datetime.fromisoformat(leg["started_at"])).total_seconds() / 60
        if elapsed >= leg["est_min"] + 2:
            advance_to_next_leg()
        else:
            leg["info"] = f"行駛中… 約 {max(0, leg['est_min'] - elapsed):.0f} 分"


# ── 主輪詢迴圈 ───────────────────────────────────────────────

def poll_loop():
    while True:
        sleep_seconds = 30
        try:
            with _lock:
                running = STATUS["running"]
                cur_idx = STATUS["current_leg"]
            if running and cur_idx >= 0:
                with _lock:
                    leg = STATUS["legs"][cur_idx]
                    ltype  = leg["type"]
                    lstatus = leg["status"]
                if lstatus in ("waiting", "on_board"):
                    sleep_seconds = 15

                if ltype == "walk" and lstatus == "walking":
                    update_walk_leg(leg)
                elif ltype == "metro":
                    if lstatus == "waiting":
                        update_metro_waiting(leg)
                    elif lstatus == "on_board":
                        update_metro_on_board(leg)
                elif ltype == "bus":
                    if lstatus == "waiting":
                        update_bus_waiting(leg)
                    elif lstatus == "on_board":
                        update_bus_on_board(leg)
                recalculate_eta()
        except Exception as e:
            with _lock:
                STATUS["error"] = str(e)
        time.sleep(sleep_seconds)


# ── Flask ────────────────────────────────────────────────────

app = Flask(__name__, static_folder=DIR)


@app.after_request
def disable_browser_cache(response):
    """開發用：避免瀏覽器持續使用修正前的 HTML/API 回應。"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/")
def index():
    return send_from_directory(DIR, "journey.html")


@app.route("/status")
def status():
    with _lock:
        return jsonify(STATUS)


@app.route("/depart", methods=["POST"])
def depart():
    """出發！開始第一段"""
    with _lock:
        if STATUS["running"]:
            return jsonify({"ok": False, "msg": "已在進行中"})
        STATUS["running"]     = True
        STATUS["current_leg"] = -1
        STATUS["started_at"]  = now_str()
        STATUS["error"]       = ""
        for leg in STATUS["legs"]:
            leg["status"]       = "pending"
            leg["info"]         = ""
            leg["vehicle_id"]   = ""
            leg["current_stop"] = ""
            leg["started_at"]   = ""
            leg["ended_at"]     = ""
            leg["boarded_at"]   = ""
            leg["lat"]          = None
            leg["lng"]          = None
    advance_to_next_leg()
    recalculate_eta()
    return jsonify({"ok": True})


@app.route("/board", methods=["POST"])
def board():
    ok = user_board()
    return jsonify({"ok": ok})


@app.route("/settings", methods=["POST"])
def settings():
    payload = request.get_json(silent=True) or {}
    try:
        height_cm = float(payload.get("height_cm"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "身高必須是數字"}), 400
    if not 120 <= height_cm <= 220:
        return jsonify({"ok": False, "msg": "身高請輸入 120–220 公分"}), 400
    apply_height_to_walk_legs(height_cm)
    with _lock:
        running = STATUS["running"]
        cur = STATUS["current_leg"]
        active_walk = (
            STATUS["legs"][cur]
            if running and cur >= 0 and STATUS["legs"][cur]["status"] == "walking"
            else None
        )
    if active_walk:
        update_walk_leg(active_walk)
    if running:
        recalculate_eta()
    else:
        with _lock:
            STATUS["eta_str"] = "--:--"
    return jsonify({
        "ok": True,
        "height_cm": STATUS["height_cm"],
        "walk_factor": round(170.0 / height_cm, 3),
    })


@app.route("/reset", methods=["POST"])
def reset():
    with _lock:
        STATUS["running"]     = False
        STATUS["current_leg"] = -1
        STATUS["eta_str"]     = "--:--"
        STATUS["started_at"]  = None
        STATUS["updated_at"]  = now_str()
        STATUS["error"]       = ""
        for leg in STATUS["legs"]:
            leg["status"]       = "pending"
            leg["info"]         = ""
            leg["vehicle_id"]   = ""
            leg["current_stop"] = ""
            leg["started_at"]   = ""
            leg["ended_at"]     = ""
            leg["boarded_at"]   = ""
            leg["lat"]          = None
            leg["lng"]          = None
    return jsonify({"ok": True})


@app.route("/map-data")
def map_data_endpoint():
    with _lock:
        cur = STATUS["current_leg"]
        return jsonify({
            "waypoints": _waypoints,
            "current_leg": cur,
        })


# ── 初始化 ───────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def init():
    global JOURNEY, CFG
    JOURNEY = json.load(open(JOURNEY_FILE, encoding="utf-8"))
    CFG     = load_config()

    # 建立 STATUS["legs"] 從 journey.json
    with _lock:
        STATUS["legs"] = [
            {
                "id":           leg["id"],
                "type":         leg["type"],
                "name":         leg["name"],
                "est_min":      leg["est_min"],
                "base_est_min": leg["est_min"],
                "system":       leg.get("system", ""),
                "line_id":      leg.get("line_id", ""),
                "from_id":      leg.get("from_id", ""),
                "from_name":    leg.get("from_name", ""),
                "to_id":        leg.get("to_id", ""),
                "to_name":      leg.get("to_name", ""),
                "direction":    leg.get("direction", 0),
                "headsign":     leg.get("headsign", ""),
                "city":         leg.get("city", ""),
                "route":        leg.get("route", ""),
                "from_stop":    leg.get("from_stop", ""),
                "to_stop":      leg.get("to_stop", ""),
                "est_stops":    leg.get("est_stops", 0),
                "notes":        leg.get("notes", ""),
                "status":       "pending",
                "info":         "",
                "vehicle_id":   "",
                "current_stop": "",
                "started_at":   "",
                "ended_at":     "",
                "boarded_at":   "",
                "lat":          None,
                "lng":          None
            }
            for leg in JOURNEY["legs"]
        ]


def main():
    init()

    # 啟動背景輪詢
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    # 背景取路線座標（地圖用）
    wt = threading.Thread(target=_fetch_waypoints_task, daemon=True)
    wt.start()

    ip = get_local_ip()
    print(f"\n🚌  通勤模擬器啟動")
    print(f"   本機：http://localhost:8080")
    print(f"   手機：http://{ip}:8080")
    print(f"   Ctrl+C 停止\n")

    app.run(host="0.0.0.0", port=8080, debug=False)


if __name__ == "__main__":
    main()
