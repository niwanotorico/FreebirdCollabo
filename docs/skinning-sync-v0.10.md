# Vertex Group / Skinning 同期（Issue #6, v0.10.0）

Issue #5（v0.9）で Armature / Bone 構造は相手側にも生成されるようになったが、スキニング済み Mesh の Vertex Group と Armature Modifier が届かず、相手側では Pose を動かしてもメッシュが変形しなかった。
v0.10 では **Vertex Group（名前・各頂点の Weight）と Armature Modifier（Armature オブジェクト参照・主要設定）** を Mesh データと一緒に双方向同期する。A 側で Automatic Weights / Weight Paint した Mesh は B 側でも同じ Group / Weight / Modifier を持ち、A 側で Pose Bone を動かすと B 側のメッシュも同じように変形する。

新しいメッセージ種別は無い。**MESH の `obj_add` / `obj_data` payload に `vg` / `w` / `mods` が乗る**だけ（`protocol.py`）。Mesh のジオメトリ再構築（`clear_geometry` + `from_pydata`）は Weight を消してしまうので、Weight はジオメトリと同じ通り道で運ぶのが正解だった（v0.9 までは Edit Mode で頂点を動かすたびに相手側の Weight が消えていた）。

## 同期するもの

| 操作 | 送るもの | 備考 |
| --- | --- | --- |
| Vertex Group の新規作成 / 削除 / Rename | `data.vg`（Group 名を index 順に） | **名前ベース**（Bone / Material と同じ）。Rename は相手には「削除＋作成」として届くが、Weight も同じ payload に乗っているので結果は一致する |
| 各頂点の Weight（Weight Paint / Automatic Weights / `vg.add`） | `data.w`（Group ごとに `[頂点 index, weight, ...]` のフラット配列） | Weight は小数 4 桁で丸める。**Weight Paint Mode 中もリアルタイム**（Mesh の Edit Mode と同じ最大 5Hz、サイズ連動の帯域制限） |
| Armature Modifier の追加 / 削除 / Rename | `data.mods`（Armature 型の Modifier だけ） | Modifier 名で対応付け。Armature 以外の Modifier は同期しない（Non-goal）。スタック内の位置（`i`）はできる範囲で合わせる |
| Modifier の Armature object 参照 | `mods[].ob`（Armature オブジェクト名） | 相手側にその Armature がまだ無ければ Modifier だけ先に作り、Armature の `obj_add` が届いた時点で参照を解決する（`_pending_mods`） |
| Modifier の設定 | `vg` (Vertex Group 絞り込み) / `uvg` (Use Vertex Groups) / `env` (Bone Envelopes) / `pv` (Preserve Volume) / `inv` (Invert) / `mm` (Multi Modifier) / `sv` (Viewport 表示) | 変わった値だけ書く（`_set`） |

Mesh の `parent` が Armature（Ctrl+P > With Automatic Weights）になる件は従来どおり `obj_add` の `parent` で届く（接続中の親変更は同期しない: 従来と同じ。Transform は `matrix_world` で送るので見た目は一致する）。

### 同期しないもの（Non-goal）

Armature 以外の Modifier（Mirror / Subdivision など）、Bone Envelope の形状（半径 / distance）、Vertex Group の index 順（名前で対応付けるので index はズレてよい）、Vertex Group のロック / 表示フラグ、Constraints / IK / Drivers、Shape Key、Keyframe / Action。

## 仕組み

### 送信側（変更検出）

- `quick_digest(MESH)` に **Group 名 + Armature Modifier 設定（`skin_meta`）と Weight** を加えた。Weight は `MeshVertex.groups` を Python で舐める（bpy 5.0 実測 1000 頂点あたり約 2ms、10 万頂点で 0.17 秒）。この digest が計算されるのは depsgraph で dirty になった Mesh と Edit / Weight Paint Mode 中の Mesh だけなので、通常の待機中にはコストは無い
- depsgraph ハンドラ: Weight の変更・Modifier の変更は Mesh の geometry 更新として立つ（従来の経路）。**Vertex Group の追加 / 削除 / Rename は geometry フラグ無しの Object 更新**しか立たないので、その場合は `_dirty_meta` に入れ、`skin_meta_digest`（Weight を含まない安い digest）が変わった時だけフル digest に進む。2 秒ごとの sweep でも同じ安い比較を全 Mesh に行う（保険）
- Weight Paint Mode 中の Mesh は Edit Mode と同じく毎回チェック対象（最大 5Hz、変わった時だけ送る）
- ログは Group / Modifier が変わった時だけ `sent skin Body (2 groups (root, tip); Armature -> Rig)`。Weight だけの変更（ペイント中）はログを出さない（5Hz でコンソールが埋まるため）

### 受信側（適用）

- ジオメトリ再構築の後に `apply_vertex_groups`（無い Group を削除 → 足りない Group を作成 → Weight 値ごとにまとめて `VertexGroup.add(..., "REPLACE")`）→ `apply_armature_modifiers`（Armature 型で名前が一致しない物を削除 → 無ければ作成 → object 参照 / 設定 / スタック位置）
- 参照先の Armature がまだ無い時は Modifier を object 無しで作り `_pending_mods` に記録。`obj_add` が届くたびに解決を試みる（`_resolve_pending_mods`）。解決時のログ: `applied armature modifier on Body (Armature -> Rig)`
- **旧バージョン（v0.9 以前）からの payload**（`vg` が無い）を受けた時は、再構築前にローカルの Weight を退避し、頂点数が同じなら戻す（相手が古くてもこちらの Weight が消えない）

### Weight Paint / Edit Mode 中の衝突（安全に保留 / 後勝ち）

| 受信側の状態 | 扱い |
| --- | --- |
| Object Mode（Armature が Pose Mode でも可） | 即適用 |
| 受信側が **同じ Mesh を Edit / Weight Paint / Sculpt / Vertex Paint 中** | 保留（`_pending_data`）。Mode を抜けた時点で、ローカルで変えていなければ適用、**変えていればローカルが勝ち**（受信した payload は捨て、ローカルを送る → 相手が適用して両側一致）。v0.9 では Mesh は Edit Mode のみ保留で「抜けた時に相手のデータで上書き」だったが、Armature と同じ後勝ちに揃えた |

`apply()` は `ob.mode != "OBJECT"` の Mesh を拒否する（v0.9 は Edit Mode だけ拒否）。Sculpt / Paint 中にブラシの下でジオメトリを作り直すのは安全ではないため。

### Pose との順序

Weight / Modifier が届く前に `pose` が届いても問題ない（Modifier が付いた時点で現在の Pose で変形する）。
v0.10 では併せて「**作った時点で既に Pose が付いている Armature**」（Pose 済みリグの Shift+D 複製、作成と同じ tick 内で Pose）の Pose が一度も送られない問題を直した: `obj_add` 直後に空のベースラインを置き、次の Pose チェックで全 Bone を 1 回送る（`sent pose GuestRig (2 bones: root, tip)`）。

### 同期ループを起こさない仕組み

Mesh の従来の仕組みそのまま: 送った / 適用した後に自分の `quick_digest` を記録し、違う時だけ送る。適用時の `vg.add` / Modifier 書き込みは depsgraph を起こすが、直後に記録した digest と一致するので送り返さない。`skin_meta` も同じく送信 / 適用時に記録する。テストで「何もしない 5 秒間に `obj_data` / `obj_add` / `pose` が 1 通も出ない」ことを両側で確認。

## 再接続

ゲストが入り直すとホストの .blend スナップショット（Vertex Group・Weight・Modifier・Pose 込み）を読み直すので、その時点で完全に一致する（変形位置まで一致することをテストで確認）。

## テスト

```
python3 tests/test_skinning.py direct      # relay / ws / wss も可。最初に unit（単一プロセス）部分を実行
blender --background --factory-startup --python tests/blender_runner.py -- tests/test_skinning.py direct   # Blender 本体で
```

自動テストの内容: 接続中に Armature + 円柱を作り Ctrl+P > With Automatic Weights → 相手側に同じ Group / Weight / Armature Modifier（object 参照込み）、
Weight Paint Mode に入ったままの Weight 変更（ライブ）、Vertex Group の追加（geometry 変更無し）/ Rename / 削除、Modifier の設定変更（Preserve Volume / Vertex Group 絞り込み / Modifier 名）、
**Pose Bone を回した時に相手側の評価済み頂点位置が一致する**（＝相手側でもメッシュが変形する）、ゲストが作ったスキニング済み Mesh + その Pose がホストに届く、
ゲストが Weight Paint 中は保留 → ゲスト自身が塗った Weight が勝ってホストに届く、Transform 同期の並行動作、再接続後の Group / Weight / Modifier / 変形位置の一致とその後の同期、アイドル時の無通信。
unit 部分: serialize → apply で Group / Weight / Modifier / 変形が再現、Rename / 削除 / Modifier 変更、安い meta digest が Weight では変わらず Group / Modifier で変わる、参照先 Armature が無い Modifier の保留と後からの解決、旧バージョン payload でローカル Weight が消えない、Weight Paint 中の適用拒否。

確認済み: **bpy 5.0.1（headless）** で direct / relay / wss PASS。既存テスト（test_sync / test_data / test_grease_pencil / test_materials / test_glb / test_pose / test_armature）も PASS。**Blender 5.2 実機で確認済み**。

### 実機（Blender 5.2）での確認手順

1. 両 PC の `scripts/addons/freebird_collab` を v0.10.0 に入れ替え（**両側とも**。System Console の `peer ... runs add-on v0.10.0` で確認）
2. ホストで Create Room → ゲスト Join Room
3. ホスト: Add > Armature、Tab で Bone を 2〜3 本押し出し、Tab で抜ける。Add > Mesh > Cylinder を Armature に重ねて置く（Bone に沿うように）
4. ホスト: Cylinder → Shift で Armature の順に選択 → Ctrl+P > **With Automatic Weights**。ゲスト側の System Console に `applied skin Cylinder (N groups (...); Armature -> Armature)` が出て、Cylinder の Properties > Data > Vertex Groups に Bone 名の Group、Modifiers に Armature（Object = Armature）が出ること
5. ホスト: Armature を選んで Ctrl+Tab で Pose Mode → Bone を R で回す → **ゲスト側の Cylinder も同じように曲がる**こと（実機ゴール 4〜5）
6. ホスト: Cylinder を選んで Ctrl+Tab で Weight Paint Mode → 塗る → ゲスト側の Weight Paint 表示（Cylinder を選んで Weight Paint Mode に入ると色で見える）が追従し、Pose を動かすと変形の違いが出ること。ホストが Weight Paint Mode のままでも届くこと
7. ホスト: Properties > Data > Vertex Groups で + で追加 / ダブルクリックで Rename / - で削除 → ゲスト側に反映（`sent skin` / `applied skin` ログ）
8. ホスト: Modifier の Preserve Volume を ON、Vertex Group 絞り込みを設定 → ゲスト側の Modifier に反映
9. ゲスト: 別の Mesh + Armature を作って Automatic Weights → ホスト側で Group / Modifier が出て、ゲストが Pose を動かすとホスト側も変形すること
10. 衝突: ゲストが Cylinder を Weight Paint Mode で塗っている間にホストも塗る → ゲスト側はモードを抜けるまで変わらない（`MESH data for Cylinder held: local mode is WEIGHT_PAINT`）→ ゲストが抜けると **ゲストの Weight がホストに届く**（`mesh Cylinder: local edits win over the data received meanwhile`）。ゲストが何も塗っていなければホストの Weight が適用される
11. ゲスト Leave Room → ホストが塗り直し / Pose 変更 → ゲスト Join し直す → Weight / Modifier / 変形が一致していること
12. System Console に `[collab] ... FAILED` が出ていないこと。Cube の移動 / Mesh Edit / マテリアル / Pose / Bone 構造の同期が従来どおり動くこと。何もしない 5 秒間に `sent skin` / `sent pose` が出ないこと

### 切り分け：相手側でメッシュが変形しない時

| 見る場所 | 意味 |
| --- | --- |
| 送信側に `sent obj_add Cylinder (MESH, ... skin: N groups (...); Armature -> Armature)` または `sent skin Cylinder (...)` が無い | 送信側で変更が検出されていない。`tick error` / `digest failed for Cylinder` が無いか見る。Cylinder が 10 万頂点を超えると Mesh ごと同期対象外（従来の制限） |
| 受信側に `applied skin Cylinder (...)` が無い | `MESH data for Cylinder held: local mode is ...`（受信側が Edit / Paint 中 → 抜けると適用）か `apply data failed for Cylinder (MESH): ...`（traceback を貼ってください） |
| `armature modifier on Cylinder waits for Armature to arrive` が出たまま | Armature の `obj_add` が届いていない（Issue #5 の切り分け表へ）。届けば `applied armature modifier on Cylinder (...)` が出る |
| Group / Modifier はあるのに変形しない | Pose が届いているか（`applied pose`）。Cylinder の Transform が一致しているか（Armature Modifier は Object の world 空間で評価する）。Modifier の `sv`（Viewport 表示）が OFF になっていないか |
| 受信側のアドオンが v0.10.0 より古い | `vg` / `w` / `mods` を無視する（Weight は届かない）。`peer ... runs add-on v0.9.x` で分かる。両側とも入れ替える |

### ログ

| ログ | 意味 |
| --- | --- |
| `sent obj_add Cylinder (MESH, 1 KB, parent Armature, skin: 2 groups (root, tip); Armature -> Rig)` | 送信側: 新規 Mesh を Skinning 付きで送った |
| `sent skin Cylinder (3 groups (root, tip, extra); Armature -> Rig)` | 送信側: Group / Modifier が変わった（Weight だけの変更ではログ無し） |
| `applied skin Cylinder (...)` | 受信側: Group / Modifier を適用した |
| `armature modifier on Cylinder waits for Rig to arrive` / `applied armature modifier on Cylinder (Armature -> Rig)` | 受信側: 参照先 Armature 待ち / 解決 |
| `MESH data for Cylinder held: local mode is WEIGHT_PAINT (active Cylinder); ...` | 受信側: ローカルが Edit / Paint 中なので保留（Object Mode に戻ると適用） |
| `mesh Cylinder: local edits win over the data received meanwhile` | 受信側: 保留中に自分も変えていたので受信分を捨て、自分のデータを送る（後勝ち） |
| `sent pose Rig (2 bones: root, tip)`（obj_add 直後） | 送信側: 作った時点で既に Pose が付いていた Armature の Pose を 1 回送った |
