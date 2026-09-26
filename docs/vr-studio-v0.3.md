# VR Studio v0.3 — VIEW（VR 内 Viewport Shading 切り替え）

v0.2（Color / Look / 右側の拡張エリア）は実機確認済み。v0.3 は **VIEW** セクションを追加：
Wireframe / Solid / Material Preview / Rendered を VR 内でワンタッチ切り替え。**自分の見え方だけ**を変えるローカル機能で、共同編集では同期しない。

## 1. 調査：ヘッドセットは何を見て描いているか

| 確認したこと | 結果 |
|---|---|
| Freebird に Shading 切り替えの既存機能はあるか | **無い**（v2.15.0 のソース全体を `shading` で検索）。`misc_utils.is_cycles_rendering()` がデスクトップ 3D Viewport の `shading.type` を**読む**だけ |
| Freebird が XR セッションに触っている設定 | `ui/__init__.py reset_viewer_settings()` が `xr_session_settings` の `show_controllers / base_scale / show_object_extras / base_pose_type` だけを変える。shading には触らない |
| VR の描画が参照する shading | **`bpy.context.window_manager.xr_session_settings.shading`**（`View3DShading`。Blender の XR セッション専用の表示設定＝VR Scene Inspection の Shading）。デスクトップの `SpaceView3D.shading` とは**別物** |
| Freebird の VR 表示との関係 | Freebird は Blender 標準の XR セッション（`wm.xr_session_toggle`）の上で動き、UI は `draw_handler(..., "XR", "POST_VIEW")` で重ねているだけ。シーン自体の描画は Blender の XR セッションが `xr_session_settings.shading` で行う |

→ **`xr_session_settings.shading.type` を切り替える**。デスクトップの 3D Viewport を変えても VR は変わらないし、その逆も同じ。

### デスクトップ画面との関係

- VR の Shading とデスクトップの 3D Viewport の Shading は**独立**。VIEW はヘッドセット側だけを変え、デスクトップ側には触らない（テストで確認）
- 選んだ Shading は VR を終了しても Blender を開いている間は残り、次に VR を開始した時もそのまま（ファイル保存時は UI 設定として .blend にも残る）

### 共同編集で同期されない理由（確認済み）

- `xr_session_settings` は **WindowManager**（UI 側）の設定で、Scene のデータではない。FreebirdCollabo は Scene のオブジェクト・マテリアル等だけを送るので、そもそも送信対象外
- ゲストが参加時にホストのシーンを開く時は `open_mainfile(load_ui=False)` なので、ゲスト自身の WindowManager（＝自分の VR Shading）がそのまま残る。bpy で実測：`load_ui=False` で開いても手元の Shading は変わらない（`load_ui=True` だとファイル側に変わる）
- 2 インスタンステストで、ホストが Wireframe → Material → Rendered と切り替えている間、`presence` / `ping` 以外のメッセージが**1 通も出ない**こと、ゲスト側は自分で選んだ Material Preview のままであることを確認

## 2. UI

- Plugins（CUSTOM）メニューに **View** ボタン。押すと VR Studio 拡張エリアに VIEW パネル（Color / Look の上に積まれる）
- 大きめの 4 ボタン（Look と同じタイルサイズ・サムネ＋名前）：Wire / Solid / Material / Rendered（ホバーで正式名称、Rendered は押すと `Rendered (EEVEE)` のようにレンダーエンジン名も表示）
- 今の Shading のボタンに青枠（Color / Look と同じ）。デスクトップの VR Scene Inspection パネル等で変えた場合も追従
- Undo の対象外（表示設定なので Undo 履歴を汚さない）

```
freebird_plugin/vr_studio/
  view.py          get_vr_shading / set_vr_shading（xr_session_settings.shading.type）
  view_section.py  VIEW パネル
  icons/view.png, view_wireframe.png, view_solid.png, view_material.png, view_rendered.png
```

## 3. 注意

- **Rendered** はシーンのレンダーエンジン（Render Properties）で描く。EEVEE なら VR でも実用的、**Cycles だとかなり重い**（両目分をパストレースするため）
- Material Preview / Rendered の見え方（HDRI・ワールド）は XR セッション側 shading の設定（既定値）に従う
- Glass の透けは v0.2 に書いた通り Raytracing が ON の時

## 4. 動作確認

```
FREEBIRD_XR_DIR=".../scripts/addons/freebird_xr" python3 tests/test_vr_studio.py ui
    # VIEW の 4 ボタンをレーザーと同じ raycast で押す → xr_session_settings.shading.type が WIREFRAME/SOLID/MATERIAL/RENDERED、
    # 青枠が追従、デスクトップの 3D Viewport の shading は不変、外部で変えた時も青枠が追従
python3 tests/test_vr_studio.py direct | relay
    # ホストが Shading を切り替えても presence/ping 以外は送られず、ゲストの Shading は自分のまま
```

確認済み：**bpy 5.0.1（headless）** で unit / ui / direct / relay PASS。
未確認：実機ヘッドセット内で切り替わる様子（headless では XR セッションを起動できないため、「VR が参照する設定を変える」ところまでの確認）。

### 実機での確認手順

1. Blender を再起動 → VR 開始 → Plugins → **View**
2. Wire → Solid → Material → Rendered を順に押し、ヘッドセット内がその場で切り替わること
3. デスクトップの 3D Viewport の Shading は変わらないこと
4. Collab で 2 人接続し、片方で切り替えてももう片方は変わらないこと
