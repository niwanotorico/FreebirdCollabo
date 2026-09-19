# 調査メモ・アーキテクチャ・リスク（2026-09-18）

## 1. Freebird XR から取得できる VR 状態

実機の `scripts/addons/freebird_xr`（v2.14.2、GPL-2.0-or-later、全て Python）を読んだ結果。

- **HMD / コントローラー**: Freebird は独自トラッキングを持たない。`bl_xr/utils/xr_session_utils.py` が Blender 標準の `window_manager.xr_session_state` を読んでいるだけ。
  - 頭: `viewer_pose_location / viewer_pose_rotation`
  - 手: `controller_aim_location_get(ctx, idx)` / `controller_aim_rotation_get` / `controller_grip_*`（idx 0 = 左, 1 = 右）
  - Freebird は受け取った quaternion を X 軸 -90° 回して「+Y が前」に直している。生の Blender/OpenXR 値は **-Z が前**。本アドオンは生の値をそのまま送り、描画側で -Z をレイ方向にしている。
- **ポインター**: `freebird/gizmos/controller_pointer.py` = main hand の aim pose から `Line(length=1)`。同じ aim pose からレイを引けば再現できる。
- **使用中ツール**: `freebird.tools.active_tool`（`"select" / "draw.stroke" / "draw.shape" / "erase" / "measure" ...`）。`sys.modules["freebird"]` から読める（Freebird は自分のフォルダを sys.path に足しているので top-level `freebird` / `bl_xr` として import 済み）。
- **選択**: Freebird の select ツールは Blender 標準の選択（`select_set`）を使う → `view_layer.objects` の `select_get()` で取れる。
- **イベント**: `bl_xr.root.add_event_listener("fb.transform_start" / "fb.transform" / "fb.transform_end" / "fb.xr_start" / "fb.xr_end" ...)` で Freebird 内部イベントを購読できる（今回は使わず、ポーリングで十分）。
- **VR 内 UI**: `freebird.api.add_launcher_button(plugin_id, button_id, label, onclick)` で VR メインメニュー CUSTOM 欄にボタンを追加できる。プラグインは `~/.freebird/plugins/*.py`（`fb_info = {"name": ...}` 必須）。
- **VR 内描画**: `bl_xr` のレンダラーは `SpaceView3D.draw_handler_add(fn, args, "XR", "POST_VIEW")` を使っているだけ。同じ API を本アドオンも直接使う → Freebird 非依存で VR ビューに描ける。
- **注意**: Freebird は `FB-Base / FB-Headset / FB-Controller-*` という Empty を作ることがある（track_xr_device_empties）。同期対象から除外（`FB-` prefix）。

## 2. Multiuser で同期できるもの

- Multiuser 0.8.2（2026-09-01）は Blender 4.5 LTS+ / 5.x 対応。オブジェクト・メッシュ・マテリアル等ほぼ全データを別プロセスの replication サーバー経由で同期。ユーザープレゼンス（カメラ視錐台・選択・モード）もあるが更新レートが低く、**XR の頭・手姿勢を載せる口がない**（fork が必要）。依存ライブラリ（zmq 等）と別サーバー起動も必要。
- 判断: MVP では使わない。「メッシュ編集まで同期したい」段階で **Multiuser にデータ同期を任せ、本アドオンは presence 専用にする**のが最短の拡張路線（本アドオンの transform 同期を切るスイッチを足すだけ）。

## 3. 不足していた機能（今回新規実装）

| 機能 | 実装 |
| --- | --- |
| ROOM 作成/参加（IP を意識させない） | `hub.py` ルームコード方式。relay サーバー or ホスト内蔵（direct） |
| JOIN 時の正本共有 | ホストが `save_as_mainfile(copy=True)` → base64 で 1 回送信 → ゲストが `open_mainfile` |
| Transform 差分同期 | 30Hz で `matrix_basis` を比較、変化分だけ `xform`。受信適用後に tracked 更新でエコー抑止 |
| 新規/削除オブジェクト | mesh は頂点/面付きで `obj_add`、その他は placeholder Empty。`obj_del` |
| Presence | 20Hz で `presence`（vr, head, hands{L,R}, tool, sel, mode） |
| 相手の描画 | `presence.py`: 頭=ワイヤーキューブ+視線、手=ピラミッド、レイ=-Z 2m、選択=bound_box、ラベル=blf（ビルボード） WINDOW と XR 両方 |
| VR 内 UI | Freebird plugin で Create/Join/Leave ボタン |
| 正本保存 | ホスト `save_mainfile`、ゲストからは `save` リクエスト |

## 4. 最小アーキテクチャ

```
[Blender A: HOST] --TCP/JSON--> [hub: relay or A 内蔵] <--TCP/JSON-- [Blender B: GUEST]
   session.tick() 60Hz timer                                  session.tick()
   ├ link.poll() 受信適用（メインスレッド）                     ├ 同左
   ├ _sync_objects() 30Hz 差分送信                              ├ 同左
   └ _send_presence() 20Hz                                      └ 同左
   presence.py draw handler (WINDOW + XR) が peers を描画
```

- 受信スレッドは queue に積むだけ。bpy を触るのはタイマー内のみ。
- ハブは「送信者以外へ転送」だけ（`to` 指定でユニキャスト）。サーバーにシーン知識なし → relay は超軽量で stdlib のみ。
- 正本 = ホストの bpy.data。ゲストは JOIN 時に上書きされる。

## 5. 技術的リスク・既知の制約

1. **同時に同じオブジェクトを掴む**と last-write-wins でガタつく。MVP は 2 人前提。必要なら `fb.transform_start/end` を購読してロック（grab owner）を送る。
2. **Edit Mode の変形は同期しない**（Transform とオブジェクト追加/削除のみ）。Freebird の draw.stroke（カーブ生成）は placeholder Empty として現れる。→ Multiuser 併用で解決予定。
3. **open_mainfile による上書き**: ゲストの未保存作業は消える（仕様）。VR セッション中に open_mainfile すると Freebird が `fb.file_load` を発火する。Freebird 起動前に JOIN する運用を推奨。
4. **相手の VR 空間の基準**: 両者の Blender ワールド座標で送っているので、Freebird のナビゲーション（移動・拡縮）を含んだ「ワールド上の位置」で正しく表示される。
5. **relay の到達性**: 相手と別ネットワークなら relay が必要（VPS 1 台、ポート 1 つ）。Tailscale 等の VPN があれば direct で足りる。
6. **Blender 5.2 実機未検証**: 自動テストは bpy 5.0.1（pip）で PASS。5.2 の XR API は同名なので動く見込みだが、実機で Phase 2〜4 の目視確認が必要。
7. `bpy.context` をタイマー内で使うため、稀にコンテキスト制限に当たる可能性 → 全て try で保護しログに出す。

## 6. 次の一手

1. 実機 2 台（または 1 台で Blender 2 個 + direct モード 127.0.0.1）で Phase 1 の動作確認
2. Freebird を両方で起動して Phase 2〜4 の目視確認（頭・手・レイ・ラベルの向き・大きさ調整）
3. 問題なければ relay を VPS に置き、遠隔 2 人で通し
