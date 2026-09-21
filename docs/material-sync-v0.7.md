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
**Blender 5.2 実機では未確認**（開発環境に 5.2 が無いため）。5.2 で上の `blender --background ...` を 1 回流すのが最短の確認。

### 実機（Blender 5.2）での確認手順

1. 両 PC の `scripts/addons/freebird_collab` を v0.7.0 に入れ替え（**両側とも**。v0.6 のままの相手には新メッセージは無視されるだけで壊れはしない）
2. Create Room → Join Room
3. Cube のマテリアルで Base Color / Roughness / Metallic / Alpha を動かす → 相手側で Material Preview 表示にして確認（両方向）
4. マテリアルを New → 名前を変える → 別のオブジェクトに割り当てる → スロットを + / − する
5. Shader Editor で Base Color に Image Texture を繋ぐ → 画像を別のものに Open し直す → リンクを外す
6. ゲストが Leave Room → ホストが色を変える → ゲストが Join し直す → 一致していること
7. System Console に `[collab] ... failed` が出ていないこと。従来どおり Transform / Edit Mode / Grease Pencil / GLB Import が動くこと
