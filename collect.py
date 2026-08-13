#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京都バス GTFS-RT 収集ボット。

1回の実行で SAMPLES 回ぶん取得し、
「日付 × 時 × 系統 × 停留所順序」ごとの集計値に丸めて CSV に追記する。

生の車両位置は保存しない（ライセンス上の再配布リスクを避けるため）。
保存するのは観測件数と混雑度の合計・最大値だけの統計値。

環境変数:
    ODPT_KEY       ODPTアクセストークン（必須）
    ODPT_FEED_URL  フィードURL（省略時は京都バスVehiclePosition）
    SAMPLES        1回の実行で取得する回数（既定 4）
    INTERVAL       取得間隔の秒数（既定 120）
    OUT_DIR        出力先ディレクトリ（既定 data）
"""
import os
import csv
import sys
import time
import datetime
import urllib.request
import urllib.error

from google.transit import gtfs_realtime_pb2

FEED_URL = os.environ.get(
    "ODPT_FEED_URL",
    "https://api.odpt.org/api/v4/gtfs/realtime/odpt_KyotoBus_AllLines_vehicle",
)
ODPT_KEY = os.environ.get("ODPT_KEY", "")
SAMPLES = int(os.environ.get("SAMPLES", "4"))
INTERVAL = int(os.environ.get("INTERVAL", "120"))
OUT_DIR = os.environ.get("OUT_DIR", "data")

JST = datetime.timezone(datetime.timedelta(hours=9))
HEADER = [
    "date", "hour", "route_id", "direction_id", "stop_sequence", "stop_id",
    "n_obs", "n_occ", "sum_occ", "max_occ",
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
    """1回取得して観測リストを返す。失敗時は空リスト（実行全体は止めない）。"""
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

    out = []
    for ent in feed.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        when = v.timestamp or feed.header.timestamp or int(time.time())
        dt = datetime.datetime.fromtimestamp(when, JST)
        # キーは必ず文字列に統一する（CSV読み戻し時と型が食い違うと二重行になるため）
        out.append({
            "key": (
                dt.strftime("%Y-%m-%d"),
                str(dt.hour),
                str(v.trip.route_id or ""),
                str(v.trip.direction_id) if v.trip.HasField("direction_id") else "",
                str(v.current_stop_sequence) if v.HasField("current_stop_sequence") else "",
                str(v.stop_id or ""),
            ),
            # 同一スナップショットの二重計上を防ぐための識別子
            "uniq": (v.vehicle.id or ent.id, v.trip.trip_id or "", when,
                     v.current_stop_sequence if v.HasField("current_stop_sequence") else -1),
            "occ": v.occupancy_status if v.HasField("occupancy_status") else None,
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
            agg[key] = [int(row["n_obs"]), int(row["n_occ"]),
                        int(row["sum_occ"]), int(row["max_occ"])]
    return agg


def save(path: str, agg: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        def order(k):
            return (k[0], int(k[1] or 0), k[2], k[3],
                    int(k[4]) if str(k[4]).isdigit() else 0)

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
        print(f"[{i + 1}/{SAMPLES}] 車両 {len(got)} 台 / 新規 {len(fresh)} 件")
        if i < SAMPLES - 1:
            time.sleep(INTERVAL)

    if not obs_all:
        print("新しい観測はありませんでした（運行時間外の可能性）")
        return

    # 日付ごとにファイルを分けて追記
    by_date = {}
    for o in obs_all:
        by_date.setdefault(o["key"][0], []).append(o)

    for date, rows in by_date.items():
        path = os.path.join(OUT_DIR, f"{date}.csv")
        agg = load_existing(path)
        for o in rows:
            rec = agg.setdefault(o["key"], [0, 0, 0, 0])
            rec[0] += 1                       # n_obs
            if o["occ"] is not None:
                rec[1] += 1                   # n_occ
                rec[2] += o["occ"]            # sum_occ
                rec[3] = max(rec[3], o["occ"])  # max_occ
        save(path, agg)
        print(f"保存: {path}（{len(agg)} 行）")


if __name__ == "__main__":
    main()
