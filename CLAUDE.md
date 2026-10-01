# CLAUDE.md — FreebirdCollabo

Claude Code がこのリポジトリで作業するときのルールと前提。

## プロジェクト

- Blender 用のリアルタイム共同編集アドオン（`freebird_collab/`）。Freebird XR は任意（VR で使うときだけ）
- `freebird_plugin/` は Freebird のプラグイン（Collab ボタン、VR Studio）。VR Studio は単独の Freebird プラグインとして配布する
- 実機環境：Blender 5.2 + Freebird XR。PC は 3090（赤羽）と 5080、リモートの相手は町田
- ライセンスは GPL-2.0-or-later（既存の SPDX ヘッダーを維持）

## やり取り

- 報告・確認・説明・エラー要約はすべて日本語で、短く。選択肢は番号付きにする
- 返答の最後に、次のアクションをはっきり書く

## Git のルール（必ず守る）

- GitHub の `main` が正史。**`main` に直接コミット・マージしない**
- 作業はブランチごとに行う（例：`issue-7-material-node-sync`、`collab-undo`）。コミットとプッシュは作業ブランチだけ
- **プッシュの前に必ずちきんに確認する**。force push は明示の OK がある時だけ
- PR は実機確認が終わってから作る
- コミットの email は `226689317+niwanotorico@users.noreply.github.com`。個人の Gmail を履歴に入れない
- README の更新は `main` への README だけのコミットにする（作業ブランチには入れない）
- `dist/` の zip はコミットしない

## 5080 PC の作業フォルダ（`Documents\ChickenOS\01_Projects\FreebirdCollabo`）

- 削除の許可は出ない。ソース・ブランチ・既存の出力は消さない
- Git 操作のあとに残る `*.lock` は削除せず、`_worktrees/_git_lock_leftovers/` に移す
- ブランチごとの作業は `_worktrees/<branch>` のワークツリーで行う

## 設計上の約束

- 同期の仕組み（`session.py` / `object_data.py` / `protocol.py`）を壊しうる変更や、Freebird の大きな改造が必要な変更は、**実装前に説明して確認を取る**
- Freebird のファイルは書き換えない。必要なら実行時のフック（元に戻せる形）にする
- VR の表示設定（View パネルのシェーディングなど）は個人ごとのローカル設定。同期しない
- 有料サービスの契約やアカウント登録はしない。サーバー・アカウント・ログインが必要になったら止めて相談する
- Direct（LAN）モードと Python リレーは動く状態を保つ
- 同期の新機能は両方の PC に同じバージョンのアドオンが必要。`ADDON_VERSION`（`session.py`）と `bl_info` をそろえて上げる

## テスト（ヘッドレス、VR 不要）

- pip 版の Blender を使う：`python3.11 -m pip install --break-system-packages bpy==4.5.14`（実機は 5.2）
- 2 プロセスのテスト：`python3.11 tests/test_sync.py direct`、`tests/test_pose.py direct`、`tests/test_materials.py direct`、`tests/test_undo.py direct` など。`relay` モードもある
- pip 版の bpy は終了時に止まることがある。テストのプロセスは最後に `os._exit()` で終わらせる（`test_undo.py` 参照）
- 止まったテストがポート 7799 をつかんだままになることがある。次を走らせる前に古いプロセスを止める
- 実機確認（VR、Freebird 本体の動作）はクラウドではできない。手順を書いてちきんに頼む
