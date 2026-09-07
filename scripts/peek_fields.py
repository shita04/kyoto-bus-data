#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GTFS-RT VehiclePosition に実際どのフィールドが入っているかを覗くだけの調査用スクリプト。

今後の設計判断（どの列を保存するか）の材料にするためのもので、
ファイルへの書き込みは一切行わない。表示するだけ。

使い方:
    python scripts/peek_fields.py

環境変数:
    ODPT_KEY       ODPTアクセストークン（未設定なら同ディレクトリの .env から読む）
    ODPT_FEED_URL  フィードURL（省略時は京都バスVehiclePosition）
"""
import os
import sys
import urllib.request

from google.transit import gtfs_realtime_pb2

FEED_URL = os.environ.get(
    "ODPT_FEED_URL",
    "https://api.odpt.org/api/v4/gtfs/realtime/odpt_KyotoBus_AllLines_vehicle",
)


def load_key() -> str:
    """トークンを環境変数、無ければ .env から読む。値は決して表示しない。"""
    key = os.environ.get("ODPT_KEY", "")
    if key:
        return key
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ODPT_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


ODPT_KEY = load_key()


def mask(text: str) -> str:
    """文字列に混ざったトークンを *** に伏せる。"""
    return text.replace(ODPT_KEY, "***") if ODPT_KEY else text


def build_url() -> str:
    if not ODPT_KEY:
        print("!! ODPT_KEY が未設定です（.env に ODPT_KEY=... を書いてください）",
              file=sys.stderr)
        sys.exit(1)
    sep = "&" if "?" in FEED_URL else "?"
    return f"{FEED_URL}{sep}acl:consumerKey={ODPT_KEY}"


def show(label: str, has: bool, value) -> None:
    mark = "Yes" if has else "No "
    print(f"  {label:<24} {mark}  {value!r}" if has else f"  {label:<24} {mark}  -")


def main() -> None:
    url = build_url()
    print(f"取得先: {mask(url)}")

    req = urllib.request.Request(url, headers={"User-Agent": "kyoto-bus-forecast/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
    except Exception as e:
        print(f"!! 取得失敗: {mask(str(e))}", file=sys.stderr)
        sys.exit(1)

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)

    vehicles = [e for e in feed.entity if e.HasField("vehicle")]
    print(f"エンティティ総数: {len(feed.entity)} / うち VehiclePosition: {len(vehicles)}")

    if not vehicles:
        print("走行中の車両が0台でした。運行時間帯に再実行してください。")
        return

    ent = vehicles[0]
    v = ent.vehicle
    t = v.trip
    print(f"\n--- 先頭1件（entity.id = {ent.id!r}）---")
    print(" [trip]")
    show("trip.trip_id", bool(t.trip_id), t.trip_id)
    show("trip.route_id", bool(t.route_id), t.route_id)
    show("trip.direction_id", t.HasField("direction_id"), t.direction_id)
    show("trip.start_date", bool(t.start_date), t.start_date)
    show("trip.start_time", bool(t.start_time), t.start_time)
    print(" [vehicle]")
    show("stop_id", bool(v.stop_id), v.stop_id)
    show("current_stop_sequence", v.HasField("current_stop_sequence"), v.current_stop_sequence)
    show("current_status", v.HasField("current_status"), v.current_status)
    show("occupancy_status", v.HasField("occupancy_status"), v.occupancy_status)


if __name__ == "__main__":
    main()
