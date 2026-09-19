# Base Color テクスチャ転送（v0.6）

## 認識の確認

正しい。JOIN 時の初期転送は `.blend` を丸ごと送るので、GLB Import で pack 済みの画像もそのまま届く。接続中の obj_add / obj_data 経路は v0.5 まで「頂点・面・材質スロット名・Base Color 値」だけで、**画像も UV も送っていなかった**。UV が無いので、仮に画像だけ送っても貼れない状態だった。

## v0.6 で送るもの

| 項目 | 送り方 | 備考 |
| --- | --- | --- |
| **UV**（active レイヤー） | mesh payload に `uv`（loop 順の 2 float）を追加 | `from_pydata` が同じ loop 順で作るのでそのまま `foreach_set`。Edit Mode 中は UV データが読めないので送らず、受信側は既存 UV を保持 |
| **Base Color 画像** | `img` メッセージ `{id: sha1, name, ext, b64}` | Principled BSDF の Base Color に繋がった Image Texture（Mix 等 1〜2 段経由も追跡）。pack 済みならその bytes、未 pack ならファイルを読む。24 MB 上限 |
| マテリアル | obj_add の `mats[i].tex = {id, name, ext}` | 受信側は `collab_id` で画像を探して Image Texture ノードを作成し Base Color に接続。Base Color 値・スロット名は従来通り |

### 再送しない仕組み

- **送信側**：画像 ID（内容の sha1）ごとにセッション中 1 回だけ送る。同じ GLB を 2 回 Import しても 2 回目は送らない（テストで確認）
- **ホスト側の追加条件**：JOIN 時のスナップショットに pack 済みで入っていた画像はゲストが既に持っているので送らない
- **受信側**：`collab_id` タグ付きの画像があれば再利用。無ければ pack 済み画像の内容ハッシュを 1 回だけ索引化して照合（スナップショット由来やローカル Import 済みの同一画像を再利用、datablock を増やさない）
- 転送は既存のチャンク分割（700 KB）にそのまま乗る。Cloudflare DO relay の 1 MiB 上限も OK

## 未対応（明示）

- Normal / Roughness / Metallic / Emission 等 Base Color 以外の PBR 入力
- Base Color の色とテクスチャの掛け合わせ（Mix ノード）は「テクスチャ優先」で単純化
- 画像の後からの差し替え（ペイント等）は再送しない（obj_add 時点の画像のみ）
- 24 MB 超の画像、Generated / UDIM 画像、Base Color 以外のテクスチャ座標（UV 以外）
- UV の編集（UV Editor で動かす）はライブ再送のトリガーにならない（頂点編集時に一緒に送られる）

## テスト（`tests/test_glb.py` に追加。direct / tcp relay / ws / wss / Cloudflare DO relay すべて PASS）

- GUEST が 8×8 チェッカー画像付き GLB を接続中に Import → HOST 側で：オブジェクト出現、UV レイヤーあり（値が非ゼロ）、Principled Base Color に Image Texture が接続、画像サイズ 8×8
- 同じ GLB を GUEST がもう一度 Import → `img` メッセージは **1 回のまま**、HOST の画像 datablock も 1 個のまま、2 個目のオブジェクトにも同じ画像が貼られる
- 回帰：`test_sync` / `test_data`（direct / wss）PASS、Edit Mode 頂点編集で UV が消えないことを確認
