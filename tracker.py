#!/usr/bin/env python3
"""
公車通勤時間追蹤器 - 813路
早上：看守所 → 中原中平路口（每台各別追蹤 + 班距統計）
晚上：中原路  → 看守所        （每台各別追蹤 + 班距統計）
"""

import requests
import time
import datetime
import json
import csv
import os
import sys
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_FILE    = os.path.join(SCRIPT_DIR, "travel_log.csv")

IS_CLOUD = os.environ.get("GITHUB_ACTIONS") == "true"

TDX_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_BASE      = "https://tdx.transportdata.tw/api/basic/v2/Bus"

_cached_token = None
_token_expiry = None


# ── 設定 ────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[錯誤] 找不到設定檔：{CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if os.environ.get("TDX_CLIENT_ID"):
        cfg["client_id"] = os.environ["TDX_CLIENT_ID"]
    if os.environ.get("TDX_CLIENT_SECRET"):
        cfg["client_secret"] = os.environ["TDX_CLIENT_SECRET"]
    return cfg


# ── TDX API ──────────────────────────────────────────────────

def get_token(cfg):
    global _cached_token, _token_expiry
    now = datetime.datetime.now()
    if _cached_token and _token_expiry and now < _token_expiry:
        return _cached_token
    r = requests.post(TDX_TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }, timeout=10)
    r.raise_for_status()
    d = r.json()
    _cached_token = d["access_token"]
    _token_expiry = now + datetime.timedelta(seconds=d.get("expires_in", 1800) - 60)
    return _cached_token


def tdx_get(path, cfg, params=None):
    token = get_token(cfg)
    p = {"$format": "JSON"}
    if params:
        p.update(params)
    r = requests.get(
        f"{TDX_BASE}/{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=p,
        timeout=10
    )
    r.raise_for_status()
    return r.json()


def get_realtime_near_stop(cfg, direction):
    return tdx_get(
        f"RealTimeNearStop/City/{cfg['city']}/{cfg['route']}",
        cfg,
        {"$filter": f"Direction eq {direction}"}
    )


def get_next_bus_eta(cfg, direction, stop_name):
    """取得下一班公車預計到站秒數與車牌"""
    try:
        data = tdx_get(
            f"EstimatedTimeOfArrival/City/{cfg['city']}/{cfg['route']}",
            cfg,
            {
                "$filter":  f"StopName/Zh_tw eq '{stop_name}' and Direction eq {direction}",
                "$orderby": "EstimateTime asc"
            }
        )
        if data:
            return data[0].get("EstimateTime"), data[0].get("PlateNumb", "?")
    except Exception:
        pass
    return None, None


def list_stops(cfg):
    """列出各時段站牌清單"""
    for sess in cfg["sessions"]:
        data = tdx_get(
            f"StopOfRoute/City/{cfg['city']}/{cfg['route']}",
            cfg,
            {"$filter": f"Direction eq {sess['direction']}"}
        )
        if not data:
            continue
        stops = data[0].get("Stops", [])
        print(f"\n【{sess['name']}】方向 {sess['direction']}  共 {len(stops)} 站")
        for i, s in enumerate(stops, 1):
            n  = s.get("StopName", {}).get("Zh_tw", "?")
            mk = "  ← 上車站" if n == sess["board_stop"] else (
                 "  ← 下車站" if n == sess["exit_stop"] else "")
            print(f"  {i:>3}. {n}{mk}")


# ── 紀錄與統計 ───────────────────────────────────────────────

def save_log(date_str, session_name, plate, board_t, exit_t, minutes):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["日期", "時段", "車牌", "上車時間", "下車時間", "行駛分鐘"])
        w.writerow([date_str, session_name, plate, board_t, exit_t, f"{minutes:.1f}"])
    print(f"\n  ✅ 記錄：[{session_name}] {plate} | {board_t} → {exit_t} | {minutes:.1f} 分鐘")


def show_stats(cfg):
    if not os.path.exists(LOG_FILE):
        return

    data_by_session = defaultdict(list)
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                data_by_session[row["時段"]].append(float(row["行駛分鐘"]))
            except (ValueError, KeyError):
                pass

    if not data_by_session:
        return

    print()
    print("━" * 50)
    for sess in cfg["sessions"]:
        name      = sess["name"]
        durations = data_by_session.get(name, [])
        if not durations:
            continue
        avg   = sum(durations) / len(durations)
        worst = max(durations)
        best  = min(durations)
        print(f"  📊 【{name}】{sess['board_stop']} → {sess['exit_stop']}（{len(durations)} 筆）")
        print(f"     平均 {avg:.0f} 分 ｜ 最快 {best:.0f} 分 ｜ 最慢 {worst:.0f} 分")
        if "work_time" in sess:
            wh, wm   = map(int, sess["work_time"].split(":"))
            work_min = wh * 60 + wm
            walk     = sess.get("walk_minutes", 0)
            dh = int((work_min - worst - 5 - walk) // 60)
            dm = int((work_min - worst - 5 - walk) % 60)
            ah = int((work_min - avg  - 5 - walk) // 60)
            am = int((work_min - avg  - 5 - walk) % 60)
            print(f"     最晚出門：{dh:02d}:{dm:02d}  ｜  平均出門：{ah:02d}:{am:02d}")
        print()
    print("━" * 50)


# ── 單一時段追蹤 ─────────────────────────────────────────────

def run_session(cfg, sess, today):
    name       = sess["name"]
    direction  = sess["direction"]
    board_stop = sess["board_stop"]
    exit_stop  = sess["exit_stop"]

    active      = {}   # plate -> board_datetime
    board_times = []   # 每次公車進站時間，用於計算班距

    print(f"\n🚌 【{name}】{board_stop} → {exit_stop}  (方向 {direction})")
    print(f"   每 {cfg['poll_interval']} 秒刷新，Ctrl+C 中止\n")

    while True:
        now_taiwan = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        now_local  = datetime.datetime.now()
        ts         = now_local.strftime("%H:%M:%S")

        if now_taiwan.hour >= sess["end_hour"]:
            print(f"\n  {sess['end_hour']}:00 時段結束。")
            break

        try:
            near = get_realtime_near_stop(cfg, direction)
        except Exception as e:
            print(f"[{ts}] ⚠ API 錯誤：{e}")
            time.sleep(cfg["poll_interval"])
            continue

        # 偵測新公車進站（上車站）
        at_board = [b for b in near
                    if b.get("StopName", {}).get("Zh_tw", "") == board_stop
                    and b.get("A2EventType") == 0]
        for bus in at_board:
            plate = bus.get("PlateNumb", "unknown")
            if plate not in active:
                active[plate] = now_local
                board_times.append(now_local)
                gap_str = ""
                if len(board_times) >= 2:
                    gap = (board_times[-1] - board_times[-2]).total_seconds() / 60
                    gap_str = f"  （距上班 {gap:.0f} 分鐘）"
                print(f"\n[{ts}] 🟢 第 {len(board_times)} 班  {plate} 進站 {board_stop}{gap_str}")

        # 偵測追蹤中的公車抵達（下車站）
        at_exit = [b for b in near
                   if b.get("StopName", {}).get("Zh_tw", "") == exit_stop
                   and b.get("A2EventType") == 0
                   and b.get("PlateNumb", "") in active]
        for bus in at_exit:
            plate    = bus.get("PlateNumb", "")
            board_dt = active.pop(plate)
            duration = (now_local - board_dt).total_seconds() / 60
            print(f"[{ts}] 🏁 {plate} 抵達 {exit_stop}，共 {duration:.1f} 分鐘")
            save_log(today, name, plate,
                     board_dt.strftime("%H:%M:%S"),
                     now_local.strftime("%H:%M:%S"),
                     duration)

        # 狀態列
        if active:
            status = ", ".join(
                f"{p}({(now_local - t).seconds // 60}分)" for p, t in active.items()
            )
            print(f"[{ts}] 🔵 追蹤中：{status}", end="\r")
        else:
            est, next_plate = get_next_bus_eta(cfg, direction, board_stop)
            if est is not None and est >= 0:
                arrive_at = now_local + datetime.timedelta(seconds=est)
                extra = ""
                if "walk_minutes" in sess:
                    dep = now_local + datetime.timedelta(
                        seconds=est - sess["walk_minutes"] * 60)
                    extra = f"  → 建議 {dep.strftime('%H:%M')} 出門"
                print(
                    f"[{ts}] 下一班 {next_plate}：{est // 60} 分後到站"
                    f"（{arrive_at.strftime('%H:%M')}）{extra}",
                    end="\r"
                )
                if est > 300:
                    sleep_sec = max(est - 180, 30)
                    print(f"\n  💤 休眠 {sleep_sec // 60} 分鐘…")
                    time.sleep(sleep_sec)
                    continue

        time.sleep(cfg["poll_interval"])

    # 班距統計
    if len(board_times) >= 2:
        intervals = [(b - a).total_seconds() / 60
                     for a, b in zip(board_times, board_times[1:])]
        avg_gap = sum(intervals) / len(intervals)
        print(f"  🕐 班距：共 {len(board_times)} 班，平均 {avg_gap:.0f} 分鐘"
              f"（最短 {min(intervals):.0f} 分 / 最長 {max(intervals):.0f} 分）")
    elif len(board_times) == 1:
        print(f"  本次追蹤到 1 班公車。")
    else:
        print(f"  本次時段內未偵測到公車。")


# ── 主程式 ───────────────────────────────────────────────────

def main():
    if "--list-stops" in sys.argv:
        cfg = load_config()
        list_stops(cfg)
        return

    cfg = load_config()

    if cfg["client_id"] in ("YOUR_TDX_CLIENT_ID", "使用 GitHub Secrets"):
        print("[錯誤] 請設定 TDX 金鑰（config.json 或 GitHub Secrets）")
        sys.exit(1)

    show_stats(cfg)
    today = datetime.date.today().strftime("%Y-%m-%d")

    now_taiwan = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    current_h  = now_taiwan.hour

    if IS_CLOUD:
        sessions_to_run = [
            s for s in cfg["sessions"]
            if s["start_hour"] - 1 <= current_h < s["end_hour"]
        ]
        if not sessions_to_run:
            print(f"[{now_taiwan.strftime('%H:%M')} 台灣時間] 不在任何追蹤時段，結束。")
            return
    else:
        sessions_to_run = cfg["sessions"]

    print(f"🚌  {cfg['route']}路  共 {len(sessions_to_run)} 個時段\n")

    try:
        for sess in sessions_to_run:
            if not IS_CLOUD:
                if datetime.datetime.now().hour >= sess["end_hour"]:
                    print(f"  【{sess['name']}】已過時段，略過。")
                    continue
                while datetime.datetime.now().hour < sess["start_hour"]:
                    now = datetime.datetime.now()
                    remain = (sess["start_hour"] - now.hour) * 60 - now.minute
                    print(f"  ⏳ 距【{sess['name']}】開始還有 {remain} 分鐘…", end="\r")
                    time.sleep(30)
            run_session(cfg, sess, today)
            show_stats(cfg)

    except KeyboardInterrupt:
        print("\n\n已中止。")
        show_stats(cfg)


if __name__ == "__main__":
    main()
