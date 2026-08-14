#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京都バス GTFS-RT TripUpdates 収集ボット。

車両位置の収集（collect.py）とは完全に独立して動く。
片方が失敗しても、もう片方には影響しない。

保存するのは「日付 × 時 × 系統 × 停留所順序」ごとの遅れの統計値のみ。
予測時刻そのものは保存しない（再配布リスクを避けるため）。

環境変数:
    ODPT_KEY   ODPTアクセストークン（必須）
    ODPT_TU_URL  フィードURL（省略時は京都バスTripUpdates）
    SAMPLES    1回の実行で取得する回数（既定 4）
    INTERVAL   取得間隔の秒数（既定 120）
    OUT_DIR    出力先ディレクトリ（既定 data_delay）
"""
import os
import csv
import sys
import time
import datetime
import urllib.request

from google.transit import gtfs_realtime_pb2

FEED_URL = os.environ.get(
    "ODPT_TU_URL",
    "https://api.odpt.org/api/v4/gtfs/realtime/odpt_KyotoBus_AllLines_trip_update",
)
ODPT_KEY = os.environ.get("ODPT_KEY", "")
SAMPLES = int(os.environ.get("SAMPLES", "4"))
INTERVAL = int(os.environ.get("INTERVAL", "120"))
OUT_DIR = os.environ.get("OUT_DIR", "data_delay")

JST = datetime.timezone(datetime.timedelta(hours=9))
HEADER = [
    "date", "hour", "route_id", "direction_id", "stop_sequence", "stop_id",
    "n_obs", "sum_delay", "max_delay", "min_delay", "n_late5",
]


def build_url() -> str:
    if "acl:consumerKey" in FEED_URL or FEED_URL.startswith("file://"):
        return FEED_URL
    if not ODPT_KEY:
        print("!! ODPT_KEY が未設定です", file=sys.stderr)
        sys.exit(1)
    sep = "&" if "?" in FEED_URL else "?"
    return f"{FEED_URL}{sep}acl:consumerKey={ODPT_KEY}"


def fetch_once(url: str):
    """1回取得して観測リストを返す。失敗しても実行全体は止めない。"""
    req = urllib.request.Request(url, headers={"User-Agent": "kyoto-bus-forecast/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
    except Exception as e:
        print(f"[warn] 取得失敗: {e}", file=sys.stderr)
        return []

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(raw)
    except Exception as e:
        print(f"[warn] 解析失敗: {e}", file=sys.stderr)
        return []

    now = feed.header.timestamp or int(time.time())
    out = []

    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        trip_delay = tu.delay if tu.HasField("delay") else None
        route = tu.trip.route_id or ""
        direction = str(tu.trip.direction_id) if tu.trip.HasField("direction_id") else ""
        trip_id = tu.trip.trip_id or ""

        for stu in tu.stop_time_update:
            # 到着側を優先し、無ければ出発側、それも無ければ便全体の遅れを使う
            delay = None
            when = None
            for ev in (stu.arrival, stu.departure):
                if ev.HasField("delay"):
                    delay = ev.delay
                if ev.HasField("time"):
                    when = ev.time
                if delay is not None:
                    break
            if delay is None:
                delay = trip_delay
            if delay is None:
                continue  # 遅れが分からないものは記録しない

            # 異常値を除外（±3時間を超える値はデータ不良とみなす）
            if abs(delay) > 3 * 3600:
                continue

            stamp = when or tu.timestamp or now
            dt = datetime.datetime.fromtimestamp(stamp, JST)
            seq = str(stu.stop_sequence) if stu.HasField("stop_sequence") else ""
            sid = stu.stop_id or ""
            if not seq and not sid:
                continue

            out.append({
                "key": (dt.strftime("%Y-%m-%d"), str(dt.hour), str(route),
                        direction, seq, str(sid)),
                # 同一スナップショットの重複を防ぐ識別子
                "uniq": (trip_id, seq, sid, stamp, delay),
                "delay": int(delay),
            })
    return out


def load_existing(path: str) -> dict:
    agg = {}
    if not os.path.exists(path):
        return agg
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["date"], row["hour"], row["route_id"],
                   row["direction_id"], row["stop_sequence"], row["stop_id"])
            agg[key] = [int(row["n_obs"]), int(row["sum_delay"]),
                        int(row["max_delay"]), int(row["min_delay"]),
                        int(row["n_late5"])]
    return agg


def save(path: str, agg: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"

    def order(k):
        return (k[0], int(k[1] or 0), k[2], k[3],
                int(k[4]) if str(k[4]).isdigit() else 0)

    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for key in sorted(agg, key=order):
            w.writerow(list(key) + agg[key])
    os.replace(tmp, path)


def main() -> None:
    url = build_url()
    seen = set()
    obs_all = []

    for i in range(SAMPLES):
        got = fetch_once(url)
        fresh = [o for o in got if o["uniq"] not in seen]
        for o in fresh:
            seen.add(o["uniq"])
        obs_all.extend(fresh)
        print(f"[{i + 1}/{SAMPLES}] 更新情報 {len(got)} 件 / 新規 {len(fresh)} 件")
        if i < SAMPLES - 1:
            time.sleep(INTERVAL)

    if not obs_all:
        print("新しい観測はありませんでした（運行時間外の可能性）")
        return

    by_date = {}
    for o in obs_all:
        by_date.setdefault(o["key"][0], []).append(o)

    for date, rows in by_date.items():
        path = os.path.join(OUT_DIR, f"{date}.csv")
        agg = load_existing(path)
        for o in rows:
            d = o["delay"]
            rec = agg.get(o["key"])
            if rec is None:
                rec = [0, 0, d, d, 0]      # n_obs, sum, max, min, n_late5
                agg[o["key"]] = rec
            rec[0] += 1
            rec[1] += d
            rec[2] = max(rec[2], d)
            rec[3] = min(rec[3], d)
            if d >= 300:                   # 5分以上の遅れ
                rec[4] += 1
        save(path, agg)
        print(f"保存: {path}（{len(agg)} 行）")


if __name__ == "__main__":
    main()
