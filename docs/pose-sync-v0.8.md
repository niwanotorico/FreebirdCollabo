# Pose Mode ボーン Transform 同期（v0.8.0）

Armature の Pose Mode でボーンを動かしたら、相手の Blender でも同じポーズになる。
前提は「両側が同じリグを持っている」こと（ゲストはホストの .blend スナップショットで同じリグを受け取る）。
既存の同期経路（変更検出 → 変わった分だけ送信 → 適用したものは「同期済み」として記録）をそのまま拡張した。

## 同期するもの

| 操作 | メッセージ | 備考 |
| --- | --- | --- |
| Pose Bone の Location / Rotation / Scale | `pose` | `{name: Armature オブジェクト名, bones: {ボーン名: {rm, l, r, s}}}`。**変わったボーンだけ**送る |
| Rotation Mode（Quaternion / Euler / Axis Angle） | `pose` の `rm` | `r` はそのモードの値そのまま（変換しない）。受信側のモードが違えば先にモードを合わせる |

対応付けは **ボーン名**（オブジェクトと同じく名前が同一性）。Armature オブジェクト自体の Transform は従来どおり `xform`。

### 受信側の安全弁

- 相手にしか無いボーン名 → そのボーンだけ無視（System Console に `pose Rig: bones not in this rig ignored: ...` を 1 回だけ出す）。他のボーンは普通に適用される
- 同名オブジェクトが Armature でない（例: 接続中にホストが Armature を追加 → 相手側にはプレースホルダの Empty ができる） → 無視して 1 回だけログ
- 受信側が同じ Armature を **Edit Mode** で編集中 → 保留して Edit Mode を抜けた時点で適用（Mesh の `obj_data` と同じ扱い）。Pose Mode 中は即適用

## 同期ループを起こさない仕組み

1. `pose_state[Armature名][ボーン名]` に「最後に送った / 適用した状態」を持ち、これと違うボーンだけ送る
2. 受信して適用したボーンはその場で `pose_state` に書き戻す → 自分の変更として送り返さない
3. 値の書き込みは実際に違うときだけ（`_set`）。同じ値の再適用で depsgraph を起こさない
4. 変更検出は depsgraph ハンドラ（Armature Object / Armature データの更新）＋ **Pose Mode 中の Armature は常にチェック**（ドラッグ中もリアルタイム）＋ 2 秒ごとの全 Armature sweep（Python API 経由の変更の保険）。チェックは最大 15Hz
5. テストで「何もしていない 5 秒間に `pose` が 1 通も出ない」ことを両側で確認

同じボーンを同時に動かした場合は後着優先（オブジェクトの `xform` と同じ）。別のボーンなら両方残る。

## 再接続

ゲストが入り直すとホストの .blend スナップショット（ポーズ込み）を読み直すので、その時点で完全に一致する。
スナップショット送信後〜読み込み完了までに届いた `pose` は保留して読み込み後に再生（`xform` 等と同じ）。
参加直後は全 Armature の現在のポーズを「同期済み」として記録するだけで、送信はしない。

## 同期しないもの（Non-goal）

Constraints、IK、Drivers、Weight Paint、Keyframe、Action / NLA。

> v0.9.0 から Armature の **新規作成と Edit Bone の構造変更（追加 / 削除 / Rename / head / tail / roll / parent / connected）** は同期される（docs/armature-sync-v0.9.md）。
> v0.8 時点では相手側にプレースホルダの Empty が出ていたが、v0.9 では実 Armature として生成され、そのまま Pose 同期が効く。

## テスト

```
python3 tests/test_pose.py direct      # relay / ws / wss も可。最初に unit（単一プロセス）部分を実行
blender --background --factory-startup --python tests/blender_runner.py -- tests/test_pose.py direct   # Blender 本体で
```

自動テストの内容: Location / Rotation（Quaternion・Euler）/ Scale の双方向、Pose Mode 中の編集、複数 Bone の連続編集、
片側にしか無い Bone を安全に無視（他の Bone は同期継続）、再接続後のポーズ一致とその後の双方向同期、
Transform / Mesh / Material 同期が並行して動くこと、アイドル時に `pose` 無通信。

確認済み: **bpy 5.0.1（headless）** で direct / relay / wss すべて PASS、既存テスト（test_sync / test_data / test_grease_pencil / test_materials / test_glb）も PASS。
**Blender 5.2 実機で確認済み**。

### 実機（Blender 5.2）での確認手順

1. 両 PC の `scripts/addons/freebird_collab` を v0.8.0 に入れ替え（**両側とも**。System Console の `peer ... runs add-on v0.8.0` で確認）
2. ホストで Armature 入りの .blend を開いて Create Room → ゲスト Join Room（リグがスナップショットで届く）
3. ホスト: Armature を選択 → Pose Mode → 腕のボーンを R で回転 → ゲスト側で同じ角度になること（ドラッグ中も追従）
4. ゲスト: 別のボーンを G で移動 / S で拡縮 → ホストに反映されること
5. 同じボーンを両側で同時に動かす → 最後に動かした側の値に揃うこと（入れ替わったまま残らないこと）
6. ゲスト Leave Room → ホストがポーズを変える → ゲスト Join し直す → 一致していること
7. System Console に `[collab] ... FAILED` が出ていないこと。Cube の移動 / Edit Mode / マテリアル変更が従来どおり同期すること

### ログ

| ログ | 意味 |
| --- | --- |
| `sent pose Rig (2 bones: hand, upper_arm)` | 送信側: 変更を検知して送った |
| `applied pose Rig from u2 (2 bones)` | 受信側: 適用した |
| `pose Rig: bones not in this rig ignored: X` | 受信側: 名前が一致しないボーンを無視（1 回だけ） |
| `pose for Rig: not an armature here (EMPTY), ignored` | 受信側: 同名オブジェクトが Armature でない |
| `apply pose Rig FAILED: ...`（traceback 付き） | 受信側: 適用で例外。このログを貼ってください |
| `pose sync error: ...`（traceback 付き） | 送信側: 検知・送信で例外（1 回だけ）。presence / ping は止まらない |
