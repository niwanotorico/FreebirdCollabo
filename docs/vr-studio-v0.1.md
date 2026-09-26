# VR Studio（Freebird プラグイン）v0.1 — VR 内カラーパレット

目標：「Blender は強力だけど VR では難しい」→「VR では触れば分かる」。
Gravity Sketch の操作感を目標に、Blender の機能を VR では極端に簡単に扱えるようにする制作 UI。
v0.1 は MVP として **32 色パレット → 選択オブジェクトへ即適用** まで。

## 1. 調査結果（Freebird XR v2.15.0 / commit 257cc94 のソースを読んだ結果）

| 知りたかったこと | 結論 | 根拠（freebird_xr 内） |
|---|---|---|
| Plugin API で VR メニューに何を足せるか | **ランチャーボタンだけ**。`add_launcher_button(plugin_id, button_id, label, onclick, icon=None)` / `remove_launcher_button`。メイン メニュー CUSTOM 欄の 4 列グリッドに並ぶ | `freebird/api.py`, `freebird/ui/custom_launcher.py` |
| ボタン以外の UI | 公式 API には無い。ただし Freebird の UI は全部同梱の **`bl_xr`**（`Node / Grid2D / Image / Text / Button / Line / Plane…`、DOM 風・CSS 風 `Node.STYLESHEET`）で組まれていて、プラグインから普通に import できる | `bl_xr/__init__.py`, `bl_xr/dom.py`, `bl_xr/ui/components.py` |
| 色スウォッチのグリッド | **作れる**。`Image(width, height, style={"background": rgb, "border_radius": r})` は src 無しで角丸の単色矩形を描く（`renderer.draw_node` の flat color rect shader）。`Grid2D` で並べる | `bl_xr/ui/renderer.py` |
| 独自パネル | **作れる**。`bl_xr.root.append_child(node)` すれば Freebird のメニューと同じ描画・レイキャスト・イベント配送に乗る。`pointer_main_enter / leave / press_end` でホバー・クリック、`apply_haptic_feedback` で振動 | `bl_xr/intersections.py`, `bl_xr/events/bind_and_dispatch.py` |
| アイコン／サムネイル | ランチャーボタンは PNG アイコン（絶対パス）可。パネル内も `Image(src=png)` で表示可。**注意**：画像のテクスチャ読込はアプリケーションタイマー内で行うと Blender が落ちる（Freebird 自身のコメント）→ サムネは XR 開始時など安全なタイミングで作る | `bl_xr/ui/components.py` Image.src |
| Freebird 本体の UI を利用・拡張 | メイン メニューは `freebird/ui/main_menu.py` のモジュール変数（`menu_group`, `submenus`）なので技術的には差し込めるが**内部実装**。v0.1 では触らず、公式 API（ランチャーボタン）＋ 自前パネル（bl_xr）に留めた | |
| 既存機能で再利用できるもの | **Undo / Redo は Freebird に既存**（左スティック左右・コントローラー上の UNDO/REDO ボタン、`freebird.undo_redo.undo/redo`）。選択は Freebird の select ツール（Blender 標準選択）。ミラーも既存（gizmo.mirror） | `freebird/undo_redo.py`, `freebird/ui/controller_panels.py` |
| プラグインの読み込み | `~/.freebird/plugins/` の `*.py` または **フォルダ（`__init__.py`）**。`fb_info = {"name": ...}` を ast で読んでから import、`register()` / `unregister()`。Blender 起動時（Freebird の register 時）に読まれる | `freebird/plugin_manager.py` |

公開 GitHub（freebirdxr）には `bl_input` しか無く、Freebird 本体の最新ソースはインストール先（`%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\freebird_xr`、GPL-2.0-or-later）を読んだ。

## 2. 設計

### 構成（Freebird 本体・FreebirdCollabo 本体とも無変更）

```
freebird_plugin/vr_studio/        ← ~/.freebird/plugins/vr_studio/ にフォルダごとコピー
  __init__.py   fb_info / register: ランチャーボタン「Color」、fb.xr_start / fb.xr_end でパネル着脱
  palette.py    32 色（sRGB hex）と sRGB→Linear 変換。bpy 非依存
  materials.py  ボタンを押した時に Blender データへ何をするか。bpy だけ（Freebird 非依存 → headless テスト可）
  panel.py      bl_xr でパネルを組む（見た目と入力だけ。ロジックは materials.py を呼ぶ）
  icons/color.png
```

UI（panel）とロジック（materials）を分けたので、Look / Asset / Modifier も「ロジック 1 モジュール + パネル 1 タブ」で足していける。

### Color の動作（「選択 → 色を押す → 終わり」）

- 対象：選択中でマテリアルを持てるオブジェクト（Mesh / Curve / Text / Surface / Meta / Grease Pencil …）。Edit Mode 中はアクティブも対象。カメラ・ライト・`FB-*` Empty は対象外
- マテリアルが無い → `VR <オブジェクト名>` を新規作成（Principled BSDF）して割り当て
- 全スロットを同じ色に塗る（Gravity Sketch と同じく「オブジェクトの色」）
- **copy-on-write**：そのマテリアルを「選択していないオブジェクト」も使っていたら、コピーしてから塗る（椅子 1 脚を塗ったら全部の椅子が変わる、を防ぐ）。選択中のオブジェクト同士で共有しているだけなら、共有のまま塗る
- Principled の Base Color を設定。Base Color にテクスチャが刺さっていたら**リンクだけ外す**（Image ノードは残る。Undo で戻る）
- Solid 表示でも見えるように `Material.diffuse_color` と `Object.color`（Freebird のペンストロークが使う）も同じ色に
- Principled が無いマテリアル（Emission だけ等）は、Material Output に繋がったシェーダーの Color を設定（ツリーは作り替えない）
- Grease Pencil マテリアルは stroke / fill の色
- 1 回押すごとに `ed.undo_push("VR colour <色名>")` → Freebird の Undo/Redo でそのまま戻せる
- 色は `fbvr_color`（Linear RGB）としてマテリアルにも記録（次の Look 用）
- 色空間：パレットは sRGB（見た目どおり）、Blender に書く値は Linear に変換

### パネル

- CUSTOM メニューの「Color」ボタンで表示／非表示
- 左手（非利き手）に追従。Freebird のメイン メニューの**サブメニューと反対側**に出る（右利きなら左側）。`fixed_scale` なのでワールドを拡大縮小しても実寸 約 19×12 cm のまま
- 8 列 × 4 段：無彩色・肌・茶 / 淡い / 鮮やか / 濃い
- ホバーで白枠＋色名表示＋軽い振動、押すと適用結果（`2 objects, 1 new` / `Select an object first` など）を表示、最後に使った色は青枠
- スウォッチはパネル背景より 3 mm 手前（bl_xr のレイキャストは 1 mm 単位で距離を丸めるので、同じ平面だと背景が勝つ）。背景にもポインターリスナーを付けてあるので、スウォッチの隙間でもレーザーが消えない

### FreebirdCollabo との関係

VR Studio は Collab を一切 import しない。Collab の Material 同期（v0.7 / v0.11）は「Blender のマテリアルが変わったら差分を送る」（depsgraph ＋ 2 秒ごとのスイープ）なので、
新規マテリアル・Base Color・リンク解除・スロット割り当てはすべて**既存の `mat` / `obj_mats` メッセージで相手に届く**。テストで確認済み（下記）。
`fbvr_color` などのカスタムプロパティは同期されないが、見た目に効く値は全部 Principled 側にあるので問題ない。

## 3. 次の段階（設計メモ）

### Look（質感プリセット、Color と独立）

- 8 種：Clay / Matte / Glossy / Plastic / Metallic / Glass / Emission / Toon
- **Look は「色以外」の Principled 値だけ**を書く（Roughness / Metallic / Coat / Specular / Transmission / Emission Strength …）。Color は Base Color だけを書く → 32×8 のマテリアルを作らず、どちらを後から変えてももう片方は残る
- Look を切り替える時は、Look が扱うパラメータの集合を毎回すべて書く（前の Look の値が残らない）
- Emission は「Emission Strength > 0 なら Color は Emission Color にも同じ色を書く」と**マテリアルの状態から判断**する（カスタムプロパティに頼らない → 相手側の人が色を変えても同じ動きになる）
- Glass は `surface_render_method` と Transmission、Toon は Shader to RGB → ColorRamp(Constant) のノード構成（v0.11 の Node Tree 同期に乗る標準ノードのみで組む）
- パネルは上部に COLOR / LOOK タブ。Look はマテリアル球のサムネイル（事前に PNG 化して起動時に読む）

### その先

| 機能 | 方針 | Collab への影響 |
|---|---|---|
| アセット読込（サムネイル） | Blender Asset Library / 指定フォルダの .blend・.glb を一覧。サムネは PNG に書き出して XR 開始時に `Image(src)` で読む。配置は `bpy.ops.wm.append` / GLB import | 追加されたオブジェクトは既存の obj_add / GLB 同期（v0.5）で届く |
| よく使う Modifier ワンタッチ | Subdivision / Solidify / Bevel / Array / Mirror（Freebird のミラーと重複させない）/ Remesh | **Armature 以外の Modifier は現状同期対象外**。VR で付けても相手には元メッシュしか見えない → Modifier 同期（Collab 本体の変更）が必要になるので、着手前に相談 |
| お気に入り | 色・Look・アセットを `~/.freebird/vr_studio.json` に保存してパネル先頭に固定 | なし |
| Undo / Redo | Freebird 既存（`freebird.undo_redo.undo/redo`）を大きめのボタンでパネルにも出すだけ | なし |

## 4. 動作確認

```
python3 tests/test_vr_studio.py unit     # 色ロジック（新規 / 同じマテリアル再利用 / copy-on-write / 共有 / 複数スロット / テクスチャ外し / Emission / GP / カーブ）
FREEBIRD_XR_DIR=".../scripts/addons/freebird_xr" python3 tests/test_vr_studio.py ui
                                         # 本物の bl_xr でパネルを組み、レーザーと同じ raycast でスウォッチに当たる → 押す → 色が付く
python3 tests/test_vr_studio.py direct   # unit + ui のあと 2 インスタンス：VR の色が既存 Collab 同期で相手に届く（両方向）
python3 tests/test_vr_studio.py relay
```

確認済み：**bpy 5.0.1（headless）** で unit / ui（Freebird v2.15.0 の bl_xr を使用）/ direct / relay PASS。既存 test_materials direct も PASS（Collab 本体は無変更）。
未確認：Blender 5.2 + Freebird 実機での見た目（パネルの位置・大きさ・スウォッチの色味）。

### 実機での確認手順

1. `freebird_plugin/vr_studio` フォルダを `C:\Users\<you>\.freebird\plugins\vr_studio` にコピー
2. Blender を再起動（Freebird はプラグインを起動時に読む）
3. VR 開始 → 左手 B（メニュー）→ CUSTOM（プラグイン）→ **Color**
4. Freebird の Select ツールでオブジェクトを選ぶ → 左手のパネルの色をレーザーで指してトリガー
5. 左スティック左で Undo できること
6. Collab で部屋に入った状態で、相手の画面の色も変わること
