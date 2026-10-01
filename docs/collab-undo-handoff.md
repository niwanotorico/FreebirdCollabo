# 引き継ぎメモ：自分専用 Undo / Redo（ブランチ `collab-undo`）

作成：2026-10-01（5080 での WIP コミット時点）

## 目的

ルーム中に Undo すると、他の人の作業まで消える問題を直す。

- 原因：Blender の Undo は「自分の操作を戻す」ではなく「ファイル全体を過去のスナップショットに戻す」。相手の変更も一緒に巻き戻り、さらにその状態が同期で相手に送られてしまう
- 目標：Gravity Sketch の共同編集のように、**Undo は自分の操作だけを打ち消す**。相手の作業には触らない
- 応急処置（Undo 後に相手の状態を再適用する案）は採用しない、とちきんが決定済み

## 仕組み

- `freebird_collab/history.py`（新規、bpy を使わない）
  - `LocalHistory`：自分が送った変更だけを「変更前 / 変更後」で記録する
  - 記録する種類：`x` 移動（ワールド行列）、`o` オブジェクトの追加・削除、`s` マテリアルスロット、`p` ポーズ（ボーン単位）、`m` マテリアル
  - ひとつながりの操作を 1 ステップにまとめる：0.8 秒操作がなければ閉じる。別のものを 0.2 秒以上あけて触ったら新しいステップ
  - 競合判定：相手の変更を適用するたびに、そのオブジェクト（またはマテリアル）のカウンターを進める。記録したあとに相手が触っていたら、その項目は戻さずに残す
  - `install_freebird_hook()`：Freebird の Undo / Redo（左スティック、コントローラーのボタン）はどれも `freebird.undo_redo._run()` を通る。ルーム中だけ、ここを差し替えて自分専用 Undo を呼ぶ。Freebird のファイルは変えない。ルームを抜けたら元に戻す
- `freebird_collab/session.py`
  - 記録する場所：`_sync_objects`（移動・追加・削除）、`_send_material_if_changed`、`_sync_materials`（スロット）、`_send_pose_if_changed`
  - 相手の変更を数える場所：`_handle` → `_note_remote`
  - `undo_local()` / `redo_local()` → `_step()` → `_revert_item()`：戻す内容を**新しい編集として**普段の同期メッセージ（xform / obj_add / obj_del / obj_mats / pose / mat）で送る。プロトコルの変更はなし
  - 削除の Undo：削除されたオブジェクトのデータ（メッシュなど）は、ファイルを開き直すまで孤立データとして残る。それを使って作り直す。そのために各オブジェクトの「戻し方」（データ名・親・コレクション・オブジェクト側のスロット・Armature モディファイア）を 0.25 秒ごとに `_restore_info` に保存している。頂点グループ名はメッシュ側に入っているので一緒に戻る
  - Edit Mode と Sculpt Mode では今まで通り Blender の Undo（その中だけで完結する）
- `freebird_collab/__init__.py`
  - `collab.undo` / `collab.redo` オペレーター。アドオンのキーマップで Ctrl+Z / Ctrl+Shift+Z に割り当てた。ルーム外や Edit Mode では poll が通らず、Blender の通常の Undo に流れる
  - COLLAB パネルに「Undo mine (n)」「Redo (n)」と直近のメッセージを表示
  - バージョンを 0.12.0 に上げた（`bl_info`、`ADDON_VERSION`）。通信の形式は変えていないので、0.11 の相手ともつながる
- `tests/test_undo.py`（新規）：2 プロセスのテスト。1 ホストの Undo で相手の Sphere が残る、2 Redo、3 競合（相手が後から触ったら戻さない）、4 追加・削除の Undo / Redo、5 マテリアル、6 ポーズ、7 ゲスト側の Undo

## テストの状況（WIP コミット時点）

- 構文チェック（py_compile）：OK
- `history.py` の簡単な単体チェック（bpy なし）：OK
- `tests/test_undo.py direct`：**まだ通っていない**
  - 1 回目：前のテストの止まり残りがポート 7799 を使っていて、ルームを作れず失敗
  - 2 回目：途中で止めた
- 既存テスト（`test_sync` / `test_pose` / `test_materials` / `test_armature` / `test_skinning` / `test_material_nodes` など）の回帰確認：**まだ**
- 実機確認：**まだ**

## 次にやること（Claude Code）

1. `python3.11 tests/test_undo.py direct` を通す。落ちたら直す（テストの段取りの問題か、実装の問題かを切り分ける）
2. `relay` モードでも `test_undo.py` を通す
3. 既存テストを一通り回して、今までの同期が壊れていないことを確かめる
4. 差分とテスト結果をちきんに報告して、プッシュの OK をもらう
5. 実機確認の手順を書く（下の案を使う）

## 実機確認の手順（案）

3090 と 5080 の両方に 0.12.0 を入れて、同じルームに入る。

1. A が Cube を動かす → B が別の Sphere を動かす → A が Undo（左スティック左）→ Cube だけ戻り、Sphere は両方の画面でそのまま
2. A が Redo（左スティック右）→ Cube がもう一度動く
3. A が Cube を動かす → B が同じ Cube を動かす → A が Undo → Cube は B の位置のまま（ログに「edited by someone else」）
4. 追加・削除：A がシェイプを描く → Undo で両方から消える → Redo で戻る。A が消しゴムで消す → Undo で戻る
5. VR Studio の Color / Look を押す → Undo で両方とも元の色に戻る
6. Pose Mode でボーンを回す → Undo
7. デスクトップの Ctrl+Z / Ctrl+Shift+Z でも同じ動きになる。Edit Mode の Ctrl+Z は今まで通りメッシュの編集だけを戻す

## わかっている制限（第 2 段の候補）

- Edit Mode のメッシュ編集・ボーン構造・ウェイト（`obj_data`）は履歴に入れていない。Edit Mode では Blender の Undo のまま。Edit Mode の始まりより前まで戻ると、Blender の Undo がファイル全体に戻す可能性がある
- 新しく作ったマテリアルそのものは Undo できない（スロットへの割り当ては Undo できる）
- 動かしている途中で 0.8 秒止まると、ステップが 2 つに分かれる
- 相手のリグのボーンに親子付けされたオブジェクトが動くと、今の同期の仕組みでは「自分の移動」として送られる。そのため自分の履歴にも入る（もとからある挙動）
- メニューの Edit > Undo を直接押すと、Blender の Undo（ファイル全体）が走る。キーと Freebird からの Undo だけを差し替えている
- ルームの外では Blender の Undo 履歴がそのまま残っている。ルームを抜けたあとに Undo すると、ルーム中の状態に戻ることがある

## 守ること

`CLAUDE.md` を参照。特に：`main` に入れない、プッシュ前に確認、noreply の email、5080 では何も削除しない。
