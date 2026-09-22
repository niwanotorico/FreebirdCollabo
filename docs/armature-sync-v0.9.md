# Armature / Bone 構造同期（Issue #5, v0.9.0）

片方の Blender で新規作成した Armature と Edit Bone の構造を、相手側の Blender にも同期する。
v0.8 まではセッション中に Armature を作ると相手側に Empty のプレースホルダが出るだけで、Pose 同期は「両側に同じリグが最初からある」ことが前提だった。
v0.9 では相手側にも **実 Armature** が生成され、Bone の追加 / 削除 / 変更が双方向に届き、その Armature に対して既存の Pose 同期がそのまま効く。

既存の `obj_add` / `obj_data` 経路をそのまま拡張した（`ARMATURE` を `object_data.SUPPORTED` に追加）。新しいメッセージ種別は無い。

## 同期するもの

| 操作 | メッセージ | 備考 |
| --- | --- | --- |
| Armature オブジェクトの新規作成 | `obj_add`（type `ARMATURE`） | payload に Bone 構造が乗る。相手側では Empty ではなく実 Armature |
| Edit Bone の追加 / 削除 | `obj_data` | 変わった時だけ最大 5Hz（Mesh と同じ経路）。**Edit Mode 中もリアルタイム** |
| Bone 名 / head / tail / roll | `obj_data` | armature 空間（rest）。位置は小数 5 桁、roll は 4 桁で丸める |
| parent / connected | `obj_data` | 親優先の順序で送る。connected の子は親の tail に head を揃える |
| Bone Rename | `obj_data` | **名前ベース**なので相手には「削除＋作成」として届く。相手側のその Bone の Pose は初期化され、送信側の現在の Pose が直後に `pose` で届く |

Bone 1 本のワイヤ形式は `{n: 名前, h: [head], t: [tail], r: roll, p: 親名 | null, c: connected}`、`data.bones` に親優先・名前順で並ぶ（`protocol.py`）。
対応付けは **Bone 名**（Object / Material と同じく名前が同一性）。Armature オブジェクト自体の Transform は従来どおり `xform`。

### 同期しないもの（Non-goal）

Weight Paint / Vertex Group、Armature Modifier、Constraints、IK、Drivers、Keyframe / Action / NLA、Custom Bone Shape、Bone Collection の詳細設定、deform / inherit などの Bone フラグ、Rigify 固有機能。
相手側で名前が同じまま残る Bone の Pose・カスタムプロパティ・コレクション所属はそのまま残る（差分適用で Bone を作り直さないため）。

## 仕組み

### 読み取り（送信側）

- Edit Mode 中の Armature は `edit_bones` から読む（ドラッグ中もリアルタイム）。それ以外は `Bone` の `head_local / tail_local / matrix_local / parent / use_connect` から読む。roll は `Bone.AxisRollFromMatrix(matrix_local)` で復元する
- 両者が同じダイジェストになるよう、Bone は名前順→親優先に並べ替え、`-0.0` は `0.0` に畳む（edit_bones と Bone は並び順と 0 の符号が違う）
- 変更検出: depsgraph ハンドラ（Armature Object の geometry 更新 / Armature データブロックの更新 — Edit Mode を抜けた時点で必ず立つ）＋ Edit Mode 中の Armature は毎回チェック（Mesh と同じ）＋ 2 秒ごとの sweep（保険）。チェックは最大 5Hz、`data_digest` が変わった時だけ送る

### 書き込み（受信側）

- Bone は Edit Mode でしか作れないので、受信側は一時的に「アクティブオブジェクトをその Armature にして Edit Mode に入り → 差分適用 → Object Mode に戻し → 元のアクティブ / 選択 / モード（同じリグの Pose Mode など）を復元」する（`object_data._edit_armature`）
- 差分適用: 相手に無い Bone を削除 → 無い Bone を作成 → 親 / connected を合わせる（親が変わる Bone は一旦 disconnect）→ head / tail / roll を変わった物だけ書く → connected を立てる。名前が同じまま残る Bone の Pose は保持される
- 新規 Armature（`obj_add`）はシーンにリンクしてから構造を適用する（Edit Mode に入るにはシーン内のオブジェクトが必要）

### Edit Mode 中の衝突（安全に保留 / 後勝ち）

| 受信側の状態 | 扱い |
| --- | --- |
| Object Mode / Pose Mode（どのオブジェクトでも） | 即適用。Pose Mode は適用後に戻す |
| 受信側が **同じ Armature を Edit Mode 中** | 保留（`_pending_data`）。Edit Mode を抜けた時点で、ローカルで構造を変えていなければ適用、**変えていればローカルが勝ち**（受信した構造は捨て、ローカルの構造を送る → 相手が適用して両側一致）。同じリグを両側で同時に組み替えた場合は「後に Edit Mode を抜けた側」が正になる |
| 受信側が **別のオブジェクトを Edit Mode / Sculpt / Paint 中** | 保留。ユーザーの Edit Mode を勝手に抜けない。Object / Pose Mode に戻った時点で適用（最新の 1 通だけ持つ） |

Mesh の `obj_data` と同じ扱いで、Pose 側の保留（`_pending_pose`）とも整合する。

### Pose 同期との順序

- Bone を追加 / Rename した直後にその Bone を動かすと、`pose` が構造（`obj_data`）より先に届いて「その Bone は無い」と捨てられてしまう。そのため **pose を送る直前にその Armature の構造を先に flush** する（`_send_pose_if_changed` → `_sync_one_object_data`）。構造がスロットル待ちなら pose も次の tick まで待つ
- 構造を送った時点で新しい Bone の現在の Pose を「同期済み」として記録する（新規 Bone の初期ポーズを無駄に送らない）。受信側も構造を適用した時点で全 Bone を「同期済み」にする → 新規 Bone の初期ポーズがエコーで相手に返らない
- 受信側で名前が残る Bone の Pose は Edit Mode を往復しても保持される（Blender の仕様）

### 同期ループを起こさない仕組み

1. 送った / 適用した構造のダイジェストを `data_digest` に記録し、これと違う時だけ送る（Mesh と同じ）
2. 受信側の書き込みは実際に違う値だけ（`_set`）。同じ構造の再適用で depsgraph を起こさない
3. 受信側で Edit Mode を往復すると Armature データブロックの更新が立つが、直後に自分のダイジェストを記録するので送り返さない
4. テストで「何もしていない 5 秒間に `obj_data` / `obj_add` / `pose` が 1 通も出ない」ことを両側で確認

## 再接続

ゲストが入り直すとホストの .blend スナップショット（Bone 構造・ポーズ込み）を読み直すので、その時点で完全に一致する。
スナップショット送信後〜読み込み完了までに届いた `obj_data` は保留して読み込み後に再生（従来どおり）。

## テスト

```
python3 tests/test_armature.py direct      # relay / ws / wss も可。最初に unit（単一プロセス）部分を実行
blender --background --factory-startup --python tests/blender_runner.py -- tests/test_armature.py direct   # Blender 本体で
```

自動テストの内容: 接続中の Armature 新規作成（相手側に実 Armature）、Edit Mode に入ったまま Bone 追加 → head / tail / roll 変更 → 削除＋Rename（ライブ）、
ゲスト側からの parent / connected 変更、ゲストが組んだ 4 本チェーン、ゲストが作ったリグに対する Pose 同期（双方向）、
Bone 追加直後の Pose（構造が先に届く）、相手が同じリグを Edit Mode 中の保留と Edit Mode 終了後の適用、
Transform / Mesh / Material 同期の並行動作、再接続後の構造＋ポーズ一致とその後の双方向同期、アイドル時の無通信。
unit 部分: edit_bones / Bone のシリアライズ一致、差分適用での Pose 保持、connected チェーン、別オブジェクト Edit Mode 中の適用拒否、Pose Mode の復元。

確認済み: **bpy 5.0.1（headless）** で direct / relay / wss すべて PASS。既存テスト（test_sync / test_data / test_grease_pencil / test_materials / test_glb / test_pose）も PASS。
test_pose の「片側にしか無い Bone」のケースは、構造が同期されるようになったので「追加した Bone とその Pose が届く」に変更した。
**Blender 5.2 実機では未確認**。

### 実機（Blender 5.2）での確認手順

1. 両 PC の `scripts/addons/freebird_collab` を v0.9.0 に入れ替え（**両側とも**。System Console の `peer ... runs add-on v0.9.0` で確認）
2. ホストで Create Room → ゲスト Join Room
3. ホスト: Add > Armature → ゲスト側にも Armature（Empty ではない）が出ること。System Console に `applied armature Armature (1 bones, 1 bones)`
4. ホスト: Tab で Edit Mode → E で押し出して Bone を数本追加 → ゲスト側に順次現れること（Edit Mode のまま）。Bone をドラッグ → 追従すること
5. ホスト: Bone を選んで X > Delete、F2 で Rename、N パネルで Roll 変更 → ゲスト側に反映
6. ゲスト: Tab で同じ Armature の Edit Mode に入り、Bone の親を変える（Ctrl+P Keep Offset / Connected）→ Tab で抜ける → ホスト側に反映
7. ゲスト: 別に Armature を新規作成して Bone を数本 → ホスト側に出ること → ホストがその Armature を Pose Mode で回転 → ゲストに反映（Pose 同期）
8. ホスト: Bone を追加してすぐ Pose Mode で動かす → ゲスト側で Bone が出て、同じポーズになること
9. 衝突: ゲストが Edit Mode 中にホストが Bone を追加 → ゲスト側は抜けるまで変わらない → Tab で抜けると追加される（ゲストが自分も構造を変えていた場合はゲストの構造がホストに届く）
10. ゲスト Leave Room → ホストが Bone を変える → ゲスト Join し直す → 一致していること
11. System Console に `[collab] ... FAILED` が出ていないこと。Cube の移動 / Mesh Edit / マテリアル / Pose 同期が従来どおり動くこと
12. 何もしない 5 秒間に `sent armature` / `sent pose` が出ないこと

### ログ

| ログ | 意味 |
| --- | --- |
| `sent obj_add Rig (ARMATURE, 0 KB)` | 送信側: 新規 Armature を構造付きで送った |
| `sent armature Rig (+tip; -arm.R; ~spine)` | 送信側: 構造の変更を送った（+ 追加 / - 削除 / ~ 変更） |
| `applied armature Rig (+tip, 4 bones)` | 受信側: 適用した |
| `armature Rig: local Edit Mode changes win over the structure received meanwhile` | 受信側: 自分の Edit Mode 中に届いた構造を捨て、自分の構造を送る（後勝ち） |
| `apply data failed for Rig: ...` | 受信側: 適用で例外。このログを貼ってください |
| `Rig (ARMATURE) sent as placeholder: ...` | 送信側: Bone 数が上限（4000）超え。相手側は Empty のプレースホルダ |
