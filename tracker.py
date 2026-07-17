#!/usr/bin/env python3
"""
公車通勤時間追蹤器
813路  看守所 → 中原中平路口
每天早上自動追蹤行駛時間，累積資料後算出最晚出門時間
"""

import requests
import time
import datetime
import json
import csv
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_FILE    = os.path.join(SCRIPT_DIR, "travel_log.csv")

# GitHub Actions 環境偵測
IS_CLOUD = os.environ.get("GITHUB_ACTIONS") == "true"

TDX_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_BASE      = "https://tdx.transportdata.tw/api/basic/v2/Bus"

_cached_token  = None
_token_expiry  = None


# ── 設定 ────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[錯誤] 找不到設定檔：{CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 雲端環境：從 GitHub Secrets 環境變數讀取金鑰（優先於 config.json）
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


def get_next_buses(cfg):
    """取得上車站下一班到站預估時間"""
    return tdx_get(
        f"EstimatedTimeOfArrival/City/{cfg['city']}/{cfg['route']}",
        cfg,
        {
            "$filter":  f"StopName/Zh_tw eq '{cfg['board_stop']}' and Direction eq {cfg['direction']}",
            "$orderby": "EstimateTime asc"
        }
    )


def get_realtime_near_stop(cfg):
    """取得各站即時公車（進/離站事件）"""
    return tdx_get(
        f"RealTimeNearStop/City/{cfg['city']}/{cfg['route']}",
        cfg,
        {"$filter": f"Direction eq {cfg['direction']}"}
    )


def list_stops(cfg):
    """列出路線所有站牌（用於確認站名和方向）"""
    data = tdx_get(
        f"StopOfRoute/City/{cfg['city']}/{cfg['route']}",
        cfg,
        {"$filter": f"Direction eq {cfg['direction']}"}
    )
    if not data:
        print("查無資料，請確認路線名稱和城市是否正確。")
        return
    stops = data[0].get("Stops", [])
    print(f"\n路線 {cfg['route']}  方向 {cfg['direction']}  共 {len(stops)} 站\n")
    for i, s in enumerate(stops, 1):
        name = s.get("StopName", {}).get("Zh_tw", "?")
        marker = ""
        if name == cfg["board_stop"]:
            marker = "  ← 上車站"
        elif name == cfg["exit_stop"]:
            marker = "  ← 下車站"
        print(f"  {i:>3}. {name}{marker}")


# ── 紀錄與統計 ───────────────────────────────────────────────

def save_log(date_str, board_t, exit_t, minutes, plate):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["日期", "上車時間", "下車時間", "行駛分鐘", "車牌"])
        w.writerow([date_str, board_t, exit_t, f"{minutes:.1f}", plate])
    print(f"\n  ✅ 已記錄：{date_str}  {board_t} → {exit_t}  共 {minutes:.1f} 分鐘  ({plate})")


def show_stats(cfg):
    if not os.path.exists(LOG_FILE):
        return

    durations = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                durations.append(float(row["行駛分鐘"]))
            except ValueError:
                pass

    if not durations:
        return

    avg   = sum(durations) / len(durations)
    worst = max(durations)
    best  = min(durations)
    n     = len(durations)

    # 最晚出門時間：以最慢車程 + 5 分緩衝計算
    wh, wm = map(int, cfg["work_time"].split(":"))
    work_min     = wh * 60 + wm
    latest_board = work_min - worst - 5          # 最晚要上車的時刻（分鐘）
    latest_depart = latest_board - cfg["walk_minutes"]
    dh = int(latest_depart // 60)
    dm = int(latest_depart % 60)

    # 平均出門時間（給個參考）
    avg_board  = work_min - avg - 5
    avg_depart = avg_board - cfg["walk_minutes"]
    ah = int(avg_depart // 60)
    am = int(avg_depart % 60)

    print()
    print("━" * 50)
    print(f"  📊  歷史統計（共 {n} 天）")
    print(f"       平均：{avg:.0f} 分  ｜  最快：{best:.0f} 分  ｜  最慢：{worst:.0f} 分")
    print()
    print(f"  🕐  最晚出門（以最慢計）：{dh:02d}:{dm:02d}")
    print(f"  🕐  平均出門（日常參考）：{ah:02d}:{am:02d}")
    print()
    print(f"       上班 {cfg['work_time']} - 最慢車程 {worst:.0f}分 - 走路 {cfg['walk_minutes']}分 - 緩衝5分")
    print("━" * 50)


# ── 主程式 ───────────────────────────────────────────────────

def main():
    # python tracker.py --list-stops  →  列出站牌清單
    if "--list-stops" in sys.argv:
        cfg = load_config()
        list_stops(cfg)
        return

    cfg = load_config()

    if cfg["client_id"] == "YOUR_TDX_CLIENT_ID":
        print("[錯誤] 請先在 config.json 填入 TDX 的 client_id 和 client_secret")
        print("  本機：編輯 config.json")
        print("  雲端：設定 GitHub Secrets TDX_CLIENT_ID / TDX_CLIENT_SECRET")
        sys.exit(1)

    show_stats(cfg)

    print(f"🚌  {cfg['route']}路  {cfg['board_stop']} → {cfg['exit_stop']}")
    print(f"📅  追蹤時段：{cfg['start_hour']:02d}:00 – {cfg['end_hour']:02d}:00")
    print(f"🔄  每 {cfg['poll_interval']} 秒刷新一次")
    print("    Ctrl+C 可隨時中止\n")

    today         = datetime.date.today().strftime("%Y-%m-%d")
    tracked_plate = None
    board_dt      = None

    try:
        while True:
            now = datetime.datetime.now()
            h   = now.hour
            ts  = now.strftime("%H:%M:%S")

            # 超過追蹤時段
            if h >= cfg["end_hour"]:
                print(f"\n{cfg['end_hour']}:00 追蹤時段結束。")
                show_stats(cfg)
                break

            # 還沒到開始時間（雲端模式下跳過此限制）
            if not IS_CLOUD and h < cfg["start_hour"]:
                remain = (cfg["start_hour"] - h) * 60 - now.minute
                print(f"  ⏳ 距追蹤開始還有約 {remain} 分鐘…", end="\r")
                time.sleep(30)
                continue

            # 取即時資料
            try:
                near = get_realtime_near_stop(cfg)
            except Exception as e:
                print(f"[{ts}] ⚠  API 錯誤：{e}")
                time.sleep(cfg["poll_interval"])
                continue

            if tracked_plate is None:
                # 尚未偵測到公車上車，顯示下一班資訊，並智慧休眠節省 API 呼叫
                try:
                    nbs = get_next_buses(cfg)
                    if nbs:
                        nb    = nbs[0]
                        est   = nb.get("EstimateTime")   # 秒
                        plate = nb.get("PlateNumb", "?")
                        if est is not None and est >= 0:
                            arrive_at   = now + datetime.timedelta(seconds=est)
                            depart_at   = now + datetime.timedelta(seconds=est - cfg["walk_minutes"] * 60)
                            print(
                                f"[{ts}] 下一班 {plate}：{est//60} 分後到站"
                                f"（{arrive_at.strftime('%H:%M')}）"
                                f"  → 建議 {depart_at.strftime('%H:%M')} 出門",
                                end="\r"
                            )
                            # 公車還很遠時，多睡一點，節省 API 額度
                            if est > 300:   # 超過 5 分鐘就先睡
                                sleep_sec = max(est - 180, 30)  # 提前 3 分鐘醒來
                                print(f"\n  💤 下一班還有 {est//60} 分鐘，休眠 {sleep_sec//60} 分鐘…")
                                time.sleep(sleep_sec)
                                continue
                except Exception:
                    pass

                # 偵測公車進站（A2EventType 0 = 進站）
                at_board = [b for b in near
                            if b.get("StopName", {}).get("Zh_tw", "") == cfg["board_stop"]
                            and b.get("A2EventType") == 0]
                if at_board:
                    bus           = at_board[0]
                    tracked_plate = bus.get("PlateNumb", "unknown")
                    board_dt      = now
                    print(f"\n[{ts}] 🟢 {tracked_plate} 進站 {cfg['board_stop']}，開始計時！")

            else:
                # 追蹤目標車是否到達下車站
                at_exit = [b for b in near
                           if b.get("StopName", {}).get("Zh_tw", "") == cfg["exit_stop"]
                           and b.get("PlateNumb", "") == tracked_plate
                           and b.get("A2EventType") == 0]

                if at_exit:
                    duration = (now - board_dt).total_seconds() / 60
                    print(f"[{ts}] 🏁 {tracked_plate} 抵達 {cfg['exit_stop']}！共 {duration:.1f} 分鐘")
                    save_log(
                        today,
                        board_dt.strftime("%H:%M:%S"),
                        now.strftime("%H:%M:%S"),
                        duration,
                        tracked_plate
                    )
                    show_stats(cfg)
                    break
                else:
                    elapsed = (now - board_dt).total_seconds() / 60
                    print(f"[{ts}] 🔵 追蹤 {tracked_plate}，已行駛 {elapsed:.0f} 分鐘…", end="\r")

            time.sleep(cfg["poll_interval"])

    except KeyboardInterrupt:
        print("\n\n已中止。")
        show_stats(cfg)


if __name__ == "__main__":
    main()
