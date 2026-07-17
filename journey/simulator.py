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
from flask import Flask, jsonify, send_from_directory

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
    r = requests.get(
        f"{TDX_BASE}/{path}",
        headers={"Authorization": "Bearer " + token},
        params=p, timeout=10
    )
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
    # 篩選正確方向
    trains = [t for t in data if headsign_kw in t.get("TripHeadSign", "")]
    if not trains:  # fallback：destination name
        trains = [t for t in data
                  if headsign_kw in (t.get("DestinationStationName") or {}).get("Zh_tw", "")]
    if not trains and data:
        trains = data  # 完全 fallback
    if not trains:
        return None
    trains.sort(key=lambda x: x.get("EstimateTime", 999))
    return trains[0].get("EstimateTime")  # 整數分鐘


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
    if data:
        est = data[0].get("EstimateTime")
        plate = data[0].get("PlateNumb", "?")
        return est, plate
    return None, None


def get_bus_near_stop(city, route, direction, cfg):
    """取得公車各站即時位置"""
    return tdx(
        f"v2/Bus/RealTimeNearStop/City/{city}/{route}",
        cfg,
        {"$filter": f"Direction eq {direction}"}
    )


# ── 狀態機 ───────────────────────────────────────────────────

JOURNEY   = None   # 從 journey.json 讀入
CFG       = None
STATUS    = {
    "running": False,
    "current_leg": -1,
    "legs": [],
    "started_at": None,
    "eta_str": "--:--",
    "updated_at": "",
    "error": ""
}
_lock = threading.Lock()


def now_str():
    return datetime.datetime.now().strftime("%H:%M:%S")


def ts():
    return datetime.datetime.now()


def eta_from_now(remaining_min):
    t = datetime.datetime.now() + datetime.timedelta(minutes=remaining_min)
    return t.strftime("%H:%M")


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
            leg["info"] = f"🚉 往{headsign}的車即將到站！"
        else:
            leg["info"] = f"下一班（往{headsign}）：{est} 分後到站"
    except Exception as e:
        leg["info"] = f"⚠ {e}"


def update_metro_on_board(leg):
    """
    在車上：以上車時刻起算倒數。
    同時查目的站 LiveBoard 作為輔助確認。
    無法用 TrainNo 追蹤特定列車，以計時器為主。
    """
    elapsed = (ts() - datetime.datetime.fromisoformat(leg["started_at"])).total_seconds() / 60
    remain  = max(0, leg["est_min"] - elapsed)

    # 查目的站確認
    try:
        est_dest = get_metro_next(
            leg["system"], leg["to_id"],
            leg.get("headsign", ""), CFG,
            line_id=leg.get("line_id")
        )
        if est_dest is not None:
            leg["info"] = f"行駛中　{leg['to_name']} 還有 {est_dest} 分"
        else:
            leg["info"] = f"行駛中　約 {remain:.0f} 分鐘到 {leg['to_name']}"
    except Exception:
        leg["info"] = f"行駛中　約 {remain:.0f} 分鐘到 {leg['to_name']}"

    # 超時自動推進（寬限 2 分鐘）
    if elapsed >= leg["est_min"] + 2:
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
        leg["info"] = f"⚠ {e}"


def update_bus_on_board(leg):
    """在車上：追蹤公車到目的站"""
    try:
        near = get_bus_near_stop(leg["city"], leg["route"], leg["direction"], CFG)
        plate = leg.get("vehicle_id", "")

        # 更新目前站
        for bus in near:
            if bus.get("PlateNumb", "") == plate:
                stop = bus.get("StopName", {}).get("Zh_tw", "")
                if stop:
                    leg["current_stop"] = stop

        # 判斷是否到達下車站
        at_dest = [b for b in near
                   if b.get("StopName", {}).get("Zh_tw", "") == leg["to_stop"]
                   and b.get("A2EventType") == 0
                   and b.get("PlateNumb", "") == plate]
        if at_dest:
            advance_to_next_leg()
            return

        elapsed = (ts() - datetime.datetime.fromisoformat(leg["started_at"])).total_seconds() / 60
        remain  = max(0, leg["est_min"] - elapsed)
        cur_stop = leg.get("current_stop", "")
        leg["info"] = f"{'📍 ' + cur_stop if cur_stop else '行駛中'}　剩約 {remain:.0f} 分"

        # fallback 計時
        if elapsed >= leg["est_min"] + 3:
            advance_to_next_leg()
    except Exception as e:
        elapsed = (ts() - datetime.datetime.fromisoformat(leg["started_at"])).total_seconds() / 60
        if elapsed >= leg["est_min"] + 3:
            advance_to_next_leg()
        else:
            leg["info"] = f"行駛中… 約 {max(0, leg['est_min'] - elapsed):.0f} 分"


# ── 主輪詢迴圈 ───────────────────────────────────────────────

def poll_loop():
    while True:
        try:
            with _lock:
                running = STATUS["running"]
                cur_idx = STATUS["current_leg"]
            if running and cur_idx >= 0:
                with _lock:
                    leg = STATUS["legs"][cur_idx]
                    ltype  = leg["type"]
                    lstatus = leg["status"]

                if ltype == "walk" and lstatus == "walking":
                    with _lock:
                        update_walk_leg(leg)
                elif ltype == "metro":
                    if lstatus == "waiting":
                        with _lock:
                            update_metro_waiting(leg)
                    elif lstatus == "on_board":
                        with _lock:
                            update_metro_on_board(leg)
                elif ltype == "bus":
                    if lstatus == "waiting":
                        with _lock:
                            update_bus_waiting(leg)
                    elif lstatus == "on_board":
                        with _lock:
                            update_bus_on_board(leg)
                recalculate_eta()
        except Exception as e:
            with _lock:
                STATUS["error"] = str(e)
        time.sleep(30)


# ── Flask ────────────────────────────────────────────────────

app = Flask(__name__, static_folder=DIR)


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
    advance_to_next_leg()
    return jsonify({"ok": True})


@app.route("/board", methods=["POST"])
def board():
    ok = user_board()
    return jsonify({"ok": ok})


@app.route("/reset", methods=["POST"])
def reset():
    with _lock:
        STATUS["running"]     = False
        STATUS["current_leg"] = -1
        STATUS["eta_str"]     = "--:--"
        for leg in STATUS["legs"]:
            leg["status"] = "pending"
            leg["info"]   = ""
    return jsonify({"ok": True})


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
                "boarded_at":   ""
            }
            for leg in JOURNEY["legs"]
        ]


def main():
    init()

    # 啟動背景輪詢
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    ip = get_local_ip()
    print(f"\n🚌  通勤模擬器啟動")
    print(f"   本機：http://localhost:8080")
    print(f"   手機：http://{ip}:8080")
    print(f"   Ctrl+C 停止\n")

    app.run(host="0.0.0.0", port=8080, debug=False)


if __name__ == "__main__":
    main()
