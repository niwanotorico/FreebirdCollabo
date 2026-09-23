# マテリアル同期（v0.7.0 / Issue #2）

Blender 間でマテリアルの変更を双方向に同期する。既存の Transform / Mesh / Grease Pencil / Texture 同期の経路
（`obj_add.mats` の状態 dict、`img` / `img_need`、digest によるエコー抑止、depsgraph ハンドラ）をそのまま拡張した。

## 同期するもの

| 操作 | メッセージ | 備考 |
| --- | --- | --- |
| Material 新規作成 | `mat` | 未使用（0 user）でも送る |
| Material 削除 | `mat_del` | 受信側も `bpy.data.materials.remove`。スロットは両側で空になる |
| Material Rename | `mat_ren` | 送信側は `session_uid` で同一性を追うので「削除＋新規」にならない（ノードも割り当ても保持） |
| Object への割り当て / Slot 追加・削除 / Slot の Link (DATA/OBJECT) | `obj_mats` | スロット列をそのまま送り、受信側を完全一致させる。面ごとの `material_index` は従来どおり Mesh 同期 |
| Base Color / Metallic / Roughness / Alpha ほか Principled BSDF の未接続入力すべて（IOR, Emission, Subsurface, Specular, Coat, Sheen, Transmission, Thin Film…） | `mat` の `p` | ソケット identifier → 値。**変わった入力だけ**送る |
| Render Method (Dithered/Blended)、Backface Culling、Viewport Display の Color / Metallic / Roughness | `mat` の `rm` `bc` `c` `vm` `vr` | Alpha を使うときの見た目を揃えるため |
| Image Texture（Base Color / Metallic / Roughness / Alpha / Normal / Emission Color に繋がったもの）の追加・差し替え・解除 | `mat` の `tex` `tx` ＋ `img` | 画像本体は内容ハッシュで 1 回だけ送信。Color Space も送る |
| Grease Pencil マテリアルのスタイル | `mat` の `gp` | 接続中の変更もライブで反映されるようになった |

### Image Texture の扱い

- 受信側にすでに Image Texture ノードが（Mix / Normal Map / Mapping 等を挟んで）繋がっていれば、**ノードグラフは触らず `node.image` だけ差し替える**。GLB 由来のマテリアルはスナップショットで同じグラフを持っているので、差し替えはこれで足りる
- 繋がっていなければ Image Texture ノードを作って接続する。Normal は Normal Map ノード経由、Alpha が Base Color と同じ画像の Alpha 出力なら同じノードを共用
- 送信側がテクスチャを外して値に戻すと、その入力が `p` に現れる → 受信側もリンクを外して値を入れる
- プロシージャルノードなど同期対象外のものが繋がった入力は `l` に入り、受信側はその入力に触らない
- 画像が手元に無ければ `img_need` で送信元に要求し、届いた時点で接続（v0.6.1 の復旧経路と同じ）
- 受け取った画像・スナップショットに入っていた画像は「相手が持っている」と見なし、マテリアルを編集しても送り返さない（GLB のマテリアルで Roughness を触っただけで 8K テクスチャが再送されるのを防ぐ）

## 同期ループを起こさない仕組み

1. 送信 / 適用のたびに、そのマテリアルのローカル状態の digest を `mat_digest` に記録。digest が変わらない限り何も送らない（受信した値を自分で適用した結果は「同期済み」として記録されるので送り返さない）
2. 値の書き込みは「実際に違うときだけ」行う（`_set`）。同じ値の再適用で depsgraph を起こさない
3. 変更検出は depsgraph ハンドラ（`Material` と `ShaderNodeTree` の更新）＋ 2 秒ごとの全マテリアル sweep（depsgraph に乗らない未使用マテリアルや Python API 経由の変更の保険）。チェックは最大 5Hz
4. テストで「何もしていない 5 秒間に `mat` / `mat_ren` / `mat_del` / `obj_mats` / `img` が 1 通も出ない」ことを両側で確認

### 同時編集

- 差分送信なので、2 人が同じマテリアルの**別のスライダー**を同時に動かしても両方残る
- リモートの状態を適用する直前に、未送信のローカル変更があれば先に送る（埋もれ防止）
- **同じスライダー**を同時に動かして値が行き違った場合は、ホストが適用後の完全な状態を送り直し、全員ホストの結果に揃う（値が入れ替わったまま残らない）

## 再接続

ゲストが入り直すとホストの .blend スナップショットを読み直すので、使用中のマテリアルはその時点で完全に一致する。
スナップショット送信後〜読み込み完了までに届いた `mat` / `mat_ren` / `mat_del` / `obj_mats` は保留して読み込み後に再生する（既存の `xform` 等と同じ扱い）。
テストでは「ゲスト離脱 → ホストが編集 → 別シーンを開いた状態のゲストが再参加 → 全マテリアルの digest が一致 → その後も双方向に同期」を確認。

## 同期しないもの（Non-goal）

> v0.11（Issue #7, `docs/material-node-sync-v0.11.md`）で標準 Shader Node の Node Tree 全体が同期対象になった。以下は v0.7 時点の記述。

任意の Shader Node Graph の完全同期、Principled BSDF 以外のシェーダーの中身、Mapping / プロシージャルテクスチャ、Custom Node Group の中身、
Geometry Nodes、Compositor、World Shader、Image の Paint 結果（ピクセル編集）、24 MB 超の画像。
マテリアルは**名前**が同一性（オブジェクトと同じ）。別々の PC で同名の無関係なマテリアルを同時に作ると同じものとして扱われる。

## テスト

```
python3 tests/test_materials.py direct      # relay / ws / wss も可。最初に unit（単一プロセス）部分を実行
blender --background --factory-startup --python tests/blender_runner.py -- tests/test_materials.py direct   # Blender 本体で
```

自動テストの内容: Base Color / Roughness / Metallic / Alpha / Emission / Render Method 双方向、新規作成（両方向）、Rename（両方向・データブロック数が変わらないこと）、
スロット追加・削除・付け替え、Image Texture 追加（sRGB + Non-Color）・差し替え（ノード再利用）・解除、削除、新規オブジェクトに載ったマテリアル、
再接続、同時編集の収束、アイドル時に無通信。

確認済み: **bpy 5.0.1（headless）** で direct / relay / ws / wss すべて PASS、既存テスト（test_sync / test_data / test_grease_pencil / test_glb）も PASS。
**Blender 5.2 実機で確認済み**。

### 実機（Blender 5.2）での確認手順

1. 両 PC の `scripts/addons/freebird_collab` を v0.7.0 に入れ替え（**両側とも**。v0.6 のままの相手には新メッセージは無視されるだけで壊れはしない）
2. Create Room → Join Room
3. Cube のマテリアルで Base Color / Roughness / Metallic / Alpha を動かす → 相手側で Material Preview 表示にして確認（両方向）
4. マテリアルを New → 名前を変える → 別のオブジェクトに割り当てる → スロットを + / − する
5. Shader Editor で Base Color に Image Texture を繋ぐ → 画像を別のものに Open し直す → リンクを外す
6. ゲストが Leave Room → ホストが色を変える → ゲストが Join し直す → 一致していること
7. System Console に `[collab] ... failed` が出ていないこと。従来どおり Transform / Edit Mode / Grease Pencil / GLB Import が動くこと

## v0.7.1: ログと切り分け（Relay 実機でマテリアルだけ同期しない件）

Relay（Python 版・Cloudflare 版とも）はメッセージ種別を見ずに全部転送するので、Relay 経路で `mat` / `obj_mats` だけ落ちることはない。
一番ありそうな原因は **片方の PC のアドオンが v0.6 のまま**（Transform / Mesh は v0.6 同士でも動くので気づきにくい）。v0.7.1 は参加時に `ver` を交換して、それを System Console に出す。

| ログ | 意味 |
| --- | --- |
| `HOST room XXXX (add-on v0.7.1, N materials tracked, M peers)` / `JOINED room ...` | 自分のバージョン。`materials tracked` が 0 でないこと |
| `peer 町田 runs add-on v0.7.1` | 相手のバージョン。**これが出れば両側 v0.7** |
| `WARNING: peer 町田 sent no add-on version in 8s: they run an add-on older than 0.7.0 ...` | **相手が旧版**。相手側のアドオンを v0.7.1 に入れ替える |
| `sent mat NAME (bsdf: Base Color, Roughness)` | 送信側: 変更を検知して送った。変わった項目が括弧内。`full` は初回の全量 |
| `sent obj_mats Cube ['MatA', 'MatB']` | 送信側: スロットの変更を送った |
| `sent material rename A -> B` / `sent material delete A` | 送信側 |
| `applied mat NAME from u2 (...)` / `applied obj_mats ...` / `applied mat_ren ...` / `applied mat_del ...` | 受信側: 適用した |
| `apply mat NAME FAILED: ...`（traceback 付き） | 受信側: 適用で例外。このログを貼ってください |
| `material sync error: ...`（traceback 付き） | 送信側: 検知・送信で例外（1 回だけ出る）。presence / ping は止まらない |
| `material NAME changed only in unsynced parts (Roughness): nothing sent` | プロシージャルノード等、同期対象外の入力しか変わっていない |

### 切り分け手順

1. 両 PC の System Console（Window > Toggle System Console）で `peer ... runs add-on v` を探す。無ければ WARNING が出ているはず → そちらの PC を v0.7.1 に
2. 変更した側に `sent mat ...` が出るか。出ない → 検知の問題（`material sync error` を探す）
3. 相手側に `applied mat ...` が出るか。出ない → 経路の問題（relay のログ、`sent` の直後の切断）
4. `applied` は出るが見た目が変わらない → ビューポートを Material Preview にする。`apply mat ... FAILED` があれば貼る
