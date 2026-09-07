# kyoto-bus-data

京都バスの GTFS-RT（リアルタイム運行情報）を定期取得し、統計値に丸めて CSV に蓄積するリポジトリ。

生の車両位置や予測時刻そのものは保存しない。保存するのは観測件数・混雑度・遅れの集計値だけ。

| スクリプト | 取得元フィード | 出力先 |
|---|---|---|
| `collect.py` | VehiclePosition（車両位置） | `data/YYYY-MM-DD.csv` |
| `collect_delay.py` | TripUpdates（遅れ情報） | `data_delay/YYYY-MM-DD.csv` |
| `scripts/peek_fields.py` | VehiclePosition | 出力なし（調査用。表示のみ） |

いずれも GitHub Actions で 10 分おきに自動実行される（日本時間 05:00〜24:59）。

## CSV の列

どちらのファイルも、先頭6列が「集計のキー」で、残りがその組み合わせに対する集計値。
同じキーの観測は 1 行にまとめられる。

### `data/YYYY-MM-DD.csv`（車両位置）

| # | 列名 | 意味 |
|---|---|---|
| 1 | `date` | 日付（日本時間） |
| 2 | `hour` | 時（0〜23） |
| 3 | `route_id` | 系統ID |
| 4 | `direction_id` | 方向（0 / 1）。上り・下りの区別 |
| 5 | `stop_sequence` | その便の中で何番目の停留所か |
| 6 | `stop_id` | 停留所ID |
| 7 | `n_obs` | 観測件数 |
| 8 | `n_occ` | 混雑度が取れた件数 |
| 9 | `sum_occ` | 混雑度の合計 |
| 10 | `max_occ` | 混雑度の最大 |

### `data_delay/YYYY-MM-DD.csv`（遅れ）

| # | 列名 | 意味 |
|---|---|---|
| 1 | `date` | 日付（日本時間） |
| 2 | `hour` | 時（0〜23） |
| 3 | `route_id` | 系統ID |
| 4 | `direction_id` | 方向（0 / 1）。上り・下りの区別 |
| 5 | `stop_sequence` | その便の中で何番目の停留所か |
| 6 | `stop_id` | 停留所ID |
| 7 | `n_obs` | 観測件数 |
| 8 | `sum_delay` | 遅れ秒数の合計 |
| 9 | `max_delay` | 遅れ秒数の最大 |
| 10 | `min_delay` | 遅れ秒数の最小 |
| 11 | `n_late5` | 5分（300秒）以上遅れた観測の件数 |

### 平均の出し方

`sum_*` はあくまで合計なので、平均が欲しいときは件数で割る。

- 平均混雑度 = `sum_occ / n_occ`（`n_occ` が 0 の行は混雑度データなし）
- 平均遅れ秒数 = `sum_delay / n_obs`

## 設計判断のメモ

### `stop_sequence` は路線の中の順番ではない

GTFS-RT の `current_stop_sequence` は「路線の中で何番目か」ではなく
**「その便（trip）の中で何番目か」** を表す。上り便も下り便も 1 から始まるので、
`route_id` と `stop_sequence` だけで集計すると、上下線の別々の停留所が同じ行に混ざる。

このリポジトリでは集計キーに `direction_id` と `stop_id` を含めているため、この混同は起きない。
停留所を特定したいときは `stop_sequence` ではなく **`stop_id` を使うこと**。

### `trip_id` はあえて保存していない

GTFS-RT のフィードには `trip_id`（便ID。「7:15発の◯◯行き」など運行1本ごとの識別子）が
入っているが、CSV には保存していない。理由は次の2点。

1. **統計値のみを保存する方針のため。** `trip_id` を集計キーに加えると、行が便ごとに
   分かれて件数がほぼ 1 になり、統計値ではなく実質的に生データの保存に近づく。
2. **区間予報に便単位の粒度が不要なため。** このデータで作るのは
   「系統 × 方向 × 停留所 × 曜日 × 時間帯」の区間予報であり、どの便かを区別する必要がない。

`scripts/peek_fields.py` を実行すれば、フィードに実際どのフィールドが入っているかを
その場で確認できる（`trip_id` / `start_date` / `start_time` なども含め、いずれも取得可能）。

### 混雑度の値についての注意

`occupancy_status` は GTFS-RT の規格で 0〜8 の区分値。**7 は `NO_DATA_AVAILABLE`（データなし）**
を意味するが、現在の `collect.py` はこれを数値 7 としてそのまま `sum_occ` / `max_occ` に
加算している。混雑度の集計値を使うときはこの点に注意すること（未対応）。

## トークンの扱い

ODPT のアクセストークンは `.env` の `ODPT_KEY` に置く。`.env` は `.gitignore` で除外済み。
GitHub Actions では リポジトリの Secrets（`ODPT_KEY`）から渡している。
URL やエラーメッセージを表示するときは、トークン部分を伏せること。
