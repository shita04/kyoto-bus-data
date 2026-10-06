#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京都バス GTFS-RT 収集ボット。

1回の実行で SAMPLES 回ぶん取得し、
「日付 × 時 × 系統 × 停留所順序」ごとの集計値に丸めて CSV に追記する。

生の車両位置は保存しない（ライセンス上の再配布リスクを避けるため）。
保存するのは観測件数と混雑度の合計・最大値だけの統計値。
混雑度は occupancy_status が 0〜6 の観測のみを対象とし、
7(NO_DATA_AVAILABLE) と 8(NOT_BOARDABLE) は件数のみ n_nodata に記録する。

便の本数（n_trips / n_trips_stand / n_trips_full）は trip_id で便を見分けて数える。
trip_id はこのプロセスのメモリの中だけで使い、CSV にもログにも出さない。
前の起動と同じ便を二重に数えないよう、前の保存から10分以内の起動では、
最初に取れた取得の時点ですでにその行にいた便を本数に数えない（見送り）。

環境変数:
    ODPT_KEY       ODPTアクセストークン（必須）
    ODPT_FEED_URL  フィードURL（省略時は京都バスVehiclePosition）
    SAMPLES        1回の実行で取得する回数（既定 4）
    INTERVAL       取得間隔の秒数（既定 120）
    OUT_DIR        出力先ディレクトリ（既定 data）
    PREV_SAVE_AT   直前のデータ保存の時刻（UNIX秒）。無い・読めないときは見送る側にする
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
PREV_SAVE_AT = os.environ.get("PREV_SAVE_AT", "")

# 前の保存からこの秒数以内の起動では、最初の取得でいた便を本数に数えない
SKIP_WINDOW = 600

JST = datetime.timezone(datetime.timedelta(hours=9))
HEADER_OLD = [
    "date", "hour", "route_id", "direction_id", "stop_sequence", "stop_id",
    "n_obs", "n_occ", "sum_occ", "max_occ", "n_nodata",
]
TRIP_COLS = ["n_trips", "n_trips_stand", "n_trips_full"]
HEADER = HEADER_OLD + TRIP_COLS

# GTFS-RT の occupancy_status（OccupancyStatus）の区分値。
#   0 EMPTY                        空いている
#   1 MANY_SEATS_AVAILABLE         座席に余裕あり
#   2 FEW_SEATS_AVAILABLE          座席わずか
#   3 STANDING_ROOM_ONLY           立ち席のみ
#   4 CRUSHED_STANDING_ROOM_ONLY   立ち席も窮屈
#   5 FULL                         満員
#   6 NOT_ACCEPTING_PASSENGERS     乗車できない（満員のため）
#   ここまでが「混雑度」として意味を持つ値。以下の2つは混雑度ではない。
#   7 NO_DATA_AVAILABLE            混雑度データが無い
#   8 NOT_BOARDABLE                そもそも乗車対象外（回送など）
# 7 は 5(FULL) より数値が大きいため、そのまま合計すると
# 「データが取れていない停留所ほど極端に混雑している」と誤って集計されてしまう。
# よって 7 と 8 は混雑度に含めず、件数だけを n_nodata に記録する。
OCC_MAX_VALID = 6


def mask(text: str) -> str:
    """文字列に混ざったトークンを *** に伏せる。"""
    return text.replace(ODPT_KEY, "***") if ODPT_KEY else text


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
        print(f"[warn] 取得失敗: {mask(str(e))}", file=sys.stderr)
        return []

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(raw)
    except Exception as e:
        print(f"[warn] 解析失敗: {mask(str(e))}", file=sys.stderr)
        return []

    out = []
    for ent in feed.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        when = v.timestamp or feed.header.timestamp or int(time.time())
        dt = datetime.datetime.fromtimestamp(when, JST)
        # 混雑度は 0〜6 のみ採用。7(データなし)と 8(乗車対象外)は別枠で数える
        occ = None
        nodata = False
        if v.HasField("occupancy_status"):
            if v.occupancy_status <= OCC_MAX_VALID:
                occ = v.occupancy_status
            else:
                nodata = True
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
            "occ": occ,
            "nodata": nodata,
            # 便の見分けにだけ使う。メモリの外（CSV・ログ）には出さない
            "trip": str(v.trip.trip_id or ""),
        })
    return out


def load_existing(path: str):
    """既存CSVを読む。戻り値は (集計, ファイルがあったか, 本数の列があったか)。"""
    agg = {}
    if not os.path.exists(path):
        return agg, False, False
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        has_trips = all(c in (reader.fieldnames or []) for c in TRIP_COLS)
        for row in reader:
            key = (row["date"], row["hour"], row["route_id"],
                   row["direction_id"], row["stop_sequence"], row["stop_id"])
            # n_nodata は後から追加した列。それ以前のCSVには存在しないので 0 とみなす
            # 本数の列は、列が無い・空欄なら None（空欄のまま。0 にはしない）
            agg[key] = [int(row["n_obs"]), int(row["n_occ"]),
                        int(row["sum_occ"]), int(row["max_occ"]),
                        int(row.get("n_nodata") or 0)] + [
                int(row[c]) if row.get(c) else None for c in TRIP_COLS]
    return agg, True, has_trips


def save(path: str, agg: dict, with_trips: bool) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER if with_trips else HEADER_OLD)
        def order(k):
            return (k[0], int(k[1] or 0), k[2], k[3],
                    int(k[4]) if str(k[4]).isdigit() else 0)

        for key in sorted(agg, key=order):
            vals = agg[key] if with_trips else agg[key][:5]
            w.writerow(list(key) + ["" if x is None else x for x in vals])
    os.replace(tmp, path)


def parse_prev_save_at(text: str):
    """PREV_SAVE_AT を UNIX秒に。無い・読めないときは None。"""
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return None


def main() -> None:
    url = build_url()
    prev = parse_prev_save_at(PREV_SAVE_AT)
    seen = set()
    obs_all = []
    # 便の記録（メモリだけ）。(行のキー, trip_id) → その行での混雑度の最大（0〜6）
    trip_max = {}
    # 見送る (行のキー, trip_id)。前の起動がすでに数えたかもしれない便
    skipped = set()
    decided = False
    gap = None

    for i in range(SAMPLES):
        got = fetch_once(url)
        # 最初に1件以上取れた取得で、見送るかどうかを1回だけ決める
        if got and not decided:
            decided = True
            if prev is not None:
                gap = int(time.time()) - prev
            if gap is None or gap < SKIP_WINDOW:
                skipped = {(o["key"], o["trip"]) for o in got
                           if o["trip"] and o["occ"] is not None}
        fresh = [o for o in got if o["uniq"] not in seen]
        for o in fresh:
            seen.add(o["uniq"])
            if o["trip"] and o["occ"] is not None:
                k = (o["key"], o["trip"])
                trip_max[k] = max(trip_max.get(k, -1), o["occ"])
        obs_all.extend(fresh)
        no_trip = sum(1 for o in got if not o["trip"])
        print(f"[{i + 1}/{SAMPLES}] 車両 {len(got)} 台 / 新規 {len(fresh)} 件"
              f" / trip_id なし {no_trip} 件")
        if i < SAMPLES - 1:
            time.sleep(INTERVAL)

    if not obs_all:
        print("新しい観測はありませんでした（運行時間外の可能性）")
        return

    # 日付ごとにファイルを分けて追記
    by_date = {}
    for o in obs_all:
        by_date.setdefault(o["key"][0], []).append(o)

    # 本数：見送り以外の便を、行ごとに1回だけ足す
    trips_by_date = {}
    n_add = [0, 0, 0]
    for (key, trip), mx in trip_max.items():
        if (key, trip) in skipped:
            continue
        add = [1, int(mx >= 3), int(mx >= 5)]
        t = trips_by_date.setdefault(key[0], {}).setdefault(key, [0, 0, 0])
        for j in range(3):
            t[j] += add[j]
            n_add[j] += add[j]

    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    for date, rows in by_date.items():
        path = os.path.join(OUT_DIR, f"{date}.csv")
        agg, existed, has_trips = load_existing(path)
        # 本数の列が無い過去の日付のファイルには、列を付けない
        with_trips = has_trips or not existed or date == today
        for o in rows:
            rec = agg.setdefault(o["key"], [0, 0, 0, 0, 0, 0, 0, 0])
            rec[0] += 1                       # n_obs
            if o["occ"] is not None:
                rec[1] += 1                   # n_occ
                rec[2] += o["occ"]            # sum_occ
                rec[3] = max(rec[3], o["occ"])  # max_occ
            elif o["nodata"]:
                rec[4] += 1                   # n_nodata（7 または 8 だった件数）
        if with_trips:
            for key, add in trips_by_date.get(date, {}).items():
                rec = agg[key]
                for j in range(3):
                    # 空欄（列を足す前の行）は空欄のまま
                    if rec[5 + j] is not None:
                        rec[5 + j] += add[j]  # n_trips / n_trips_stand / n_trips_full
        save(path, agg, with_trips)
        print(f"保存: {path}（{len(agg)} 行）")

    since = f"前の保存から {gap} 秒" if gap is not None else "前の保存の時刻なし"
    print(f"便 +{n_add[0]} / 立ち +{n_add[1]} / 満員 +{n_add[2]}"
          f" / 見送り {len(skipped)}（{since}）")


if __name__ == "__main__":
    main()
