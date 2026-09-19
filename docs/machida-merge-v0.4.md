# 町田修正の統合レビュー（v0.4）

## 結論

- 町田側の**原因分析は妥当**：v0.1〜v0.3 は「オブジェクトの Transform」と「新規メッシュの追加」しか監視しておらず、メッシュの頂点編集や Curve / Light / Camera 等のデータ変更は一切送っていなかった
- 町田版 ZIP は **v0.1 ベース**（`ws.py` なし・Relay URL なし・700KB チャンク分割なし・keepalive ping なし）。そのままの上書きは v0.2〜v0.3 の全機能を失うので不可
- 実質的な貢献は `object_data.py`（型別シリアライズ）と session の「データ同期」の考え方。**これを v0.3 に移植し、検知方式と適用方式を作り直した**のが v0.4

## 採用 / 不採用

| 町田修正の要素 | 採否 | 理由 |
| --- | --- | --- |
| `object_data.py`：Mesh / Curve / Text / Light / Camera / Empty の型別シリアライズ・復元 | **採用（改変）** | 設計は良い。フィールドを短縮し、Mesh に `material_index` を追加 |
| `obj_data` メッセージ（データ変更を Transform と別経路で送る） | **採用** | プロトコルは v0.3 と互換のまま追加できる |
| Edit Mode 中は `update_from_editmode()` で bmesh の編集をメッシュに反映してから読む | **採用** | これが無いと Edit Mode 中の頂点移動は見えない |
| 削除時に fingerprint も掃除、`leave()` で `_scene_loaded_from_host` / `_pending_scene` をリセット | **採用** | 再 JOIN 時のバグ防止。良い指摘 |
| ゲストはホストのシーンを受け取るまで自分のオブジェクトを送らない | **採用** | v0.3 の潜在バグ（JOIN 直後に古いシーンのオブジェクトをホストへ押し込む可能性）を塞ぐ |
| Surface（NURBS 面）の spline 復元 | **不採用 → 評価済みメッシュで送る** | `Spline.point_count_u/v` は Python から書けない（read-only）。町田版でも復元は失敗する。v0.4 は SURFACE / META を評価後メッシュとして送り、受信側は同名の MESH になる（形は正しい） |
| **5Hz で全オブジェクトをフル JSON 化 → SHA256** の変化検知 | **不採用 → depsgraph ハンドラで「変わった物だけ」** | 10 万頂点メッシュだと毎 200ms に数百 ms メインスレッドが止まり VR が破綻する。v0.4 は `depsgraph_update_post` の `is_updated_geometry` で dirty にした物だけ、`foreach_get` の高速ハッシュ（5 万頂点で 20ms）→ 変化時のみ JSON 化（同 257ms） |
| 受信側で **datablock を丸ごと差し替え**（`ob.data = new`） | **不採用 → in-place 更新** | 差し替えるとマテリアル割当・カスタムプロパティが消え、`.001` の孤児データが増える。相手が Edit Mode 中だと例外。v0.4 は `clear_geometry()+from_pydata()` / `splines.clear()` / `setattr` で既存 datablock を更新、Edit Mode 中のオブジェクトへの適用は Edit Mode を抜けるまで保留 |
| 帯域制限なし | **不採用 → サイズ連動スロットル** | オブジェクト毎に `max(1/5Hz, bytes / 1.5MB/s)` の最短再送間隔。2.7MB のメッシュをドラッグ中でも最大 1.8 秒に 1 回。連続編集は最後の状態にまとめて送る |
| tick 内の deferred 処理の書き換え | **不採用** | v0.3 に同等の実装（スナップショット適用前の編集を保留→再生）が既にある |
| `__init__.py` / `hub.py` / `link.py` / `protocol.py` の差分 | **不採用** | すべて v0.1 への巻き戻し（Relay URL → host/port、WebSocket 削除など） |

## v0.4 の変更検知と送信量

```
depsgraph_update_post ──► dirty {名前}          （ポーリングなし。変わった物しか触らない）
        │                        │
        │   Edit Mode 中の物 ────┤  毎 200ms 再確認（bmesh → mesh 反映が必要なため）
        │   Light/Camera/Empty/Text ┤  2 秒毎の安全網（小さいので無視できる）
        ▼                        ▼
   quick_digest（foreach_get ハッシュ）== 前回 ?  ──yes──► 何もしない
        │ no
        ▼
   serialize → obj_data 送信 → 次回送信可能時刻 = now + max(0.2s, bytes/1.5MB/s)
```

計測（bpy 5.0.1）：50,946 頂点 → digest 20ms / serialize 257ms / 2.67MB / 最短 1.78s 間隔。3,010 頂点 → 2ms / 17ms / 143KB / 0.2s。
`tests/test_data.py` の実測：7 種追加 31KB、6 回の更新 1.5KB、**アイドル 3 秒間の送信 0**。JSON 化はすでに送信スレッドで行われるため、メインスレッド負荷は Python リスト構築分のみ。

## 同期対象（v0.4）

| 種類 | 追加 | 内容更新 | 備考 |
| --- | --- | --- | --- |
| MESH | ○ | ○（Edit Mode 中もリアルタイム） | 頂点・ルーズ辺・面・face の material_index。10 万頂点上限 |
| CURVE | ○ | ○ | Bezier / NURBS / Poly の点・ハンドル・cyclic・bevel・extrude |
| FONT（Text） | ○ | ○ | body / size / extrude / bevel / align |
| LIGHT | ○ | ○ | type / energy / color / soft size / spot |
| CAMERA | ○ | ○ | type / lens / ortho / clip |
| EMPTY | ○ | ○ | display type / size |
| SURFACE / META | ○（評価後メッシュとして） | ○ | 受信側は MESH になる（API 制約） |
| その他（Armature, GreasePencil 等） | placeholder Empty | × | 名前と位置だけ |

**制約**：UV / 法線 / 頂点グループ / シェイプキー / モディファイア / マテリアル本体は送らない（受信側の既存 datablock のものは保持される）。両者が同じメッシュを同時に編集すると後勝ち。

## テスト（すべて PASS）

- 回帰 `tests/test_sync.py`：direct / relay(tcp) / ws / wss / Cloudflare DO relay
- 新規 `tests/test_data.py`：direct / relay / ws / wss / Cloudflare DO relay — Mesh 追加、Edit Mode 頂点編集（ホスト→ゲスト、ゲスト→ホスト、編集中に同期）、Curve、Surface、Text、Light、Camera、Empty、プロパティ更新、削除、Transform、アイドル時の送信ゼロ、材質スロット保持
- アドオン register / unregister
