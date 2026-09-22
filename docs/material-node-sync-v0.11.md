# Material Node Tree 同期（Issue #7, v0.11.0）

v0.7（Issue #2）のマテリアル同期は Principled BSDF の未接続入力と、そこに直接繋いだ Image Texture までが対象だった。
v0.11 では **Blender 標準 Shader Node の Node Tree 全体**（ノードの追加 / 削除、種類、名前、位置、主要 input 値、ノードのプロパティ、ノード間リンク、Material Output への接続）を双方向同期する。
Noise → ColorRamp → Base Color のようなプロシージャル構成、Mix Shader で Emission / Glass / Transparent を混ぜた構成、Mapping / Texture Coordinate / Normal Map / Bump の配線が相手側にもそのまま再現される。

新しいメッセージ種別は無い。**`mat` の状態 dict（`obj_add.mats` にも同じ物が乗る）に `nt` キーが増えるだけ**（`protocol.py`）。
既存の Principled 用キー（`p` / `tex` / `tx` / `l`）はそのまま残す。v0.10 以前の相手は `nt` を知らないので無視し、従来どおり Principled の値とテクスチャだけが届く（壊れない）。

## payload（`object_data.py` の node tree セクション）

```
nt = {
  "nodes": {ノード名: {"t": bl_idname,            # 例 ShaderNodeTexNoise
                      "l": [x, y],                # 位置
                      "lb": label, "mu": True,    # ラベル / Mute（既定値の時は省略）
                      "i": {input identifier: 値},  # 値を持つ全 input（リンク中でも送る。float / int / bool / str / 配列）
                      "o": {output identifier: 値}, # Value / RGB ノードだけ（値が output 側にある）
                      "pr": {プロパティ名: 値},     # ノード固有の enum / bool / int / float / string
                                                  #   noise_dimensions, blend_type, vector_type, space, invert, is_active_output...
                      "img": {"name", "path", "src", "cs", "id"?},  # Image Texture: 参照だけ（v1）
                      "ramp": {"cm", "ip", "hi", "el": [[pos, [rgba]], ...]}}},  # ColorRamp
  "links": [[from node, from socket identifier, to node, to socket identifier], ...],  # ソート済み
  "x": [同期対象外のローカルノード名]   # Node Group / OSL / 外部 Addon ノード
}
```

- ノードの対応付けは**名前**（Material / Bone / Vertex Group と同じ）。同名で種類が違えば作り直す
- `pr` は `bpy.types.ShaderNode` 共通のプロパティ（name / location / inputs…）を除いた、そのノード固有の単純プロパティを RNA から自動列挙する。**ノード種別ごとの個別コードは無い**ので、Math / Vector Math / Mix / Separate-Combine / Fresnel / Layer Weight など標準ノードは一通り乗る（優先確認済み: Principled / Emission / Diffuse / Glass / Transparent / Mix Shader / ColorRamp / Noise / Voronoi / Wave / Mapping / Texture Coordinate / Normal Map / Bump / Image Texture / RGB / Value）
- 差分送信: `material_delta` は `nt` を **ノード単位 → キー単位**で比較し、変わったノードの変わった `i` / `pr` / `l` だけ、消えたノードは `del`、リンクは変わった時だけ全リストを `"d": 1` 付きで送る。二人が同じノードの別の入力を同時に触っても両方残る（テスト済み）。同じ入力が行き違った時は v0.7 と同じくホストが全量を送り直して揃える
- 全量（`d` 無し）を受けた側は、payload に無い**同期対象ノードだけ**を削除する。相手の `x` に載っているノード、こちらにしかない Node Group 等は残す

### Image Texture（v1 は参照のみ）

`img` は画像の名前 / ファイルパス / source / Color Space。**画像本体は送らない**。受信側は (1) 内容ハッシュ `id`（Principled に繋いだ画像で v0.7 の経路が既に運んだ物）→ (2) 同名の画像 → (3) パスがこちらに存在すればロード、の順で解決する。
どれも無ければ Image Texture ノードは**画像なしのまま作り**、System Console に 1 回だけ
`material X: image 'Y' (path) is not available here; the Image Texture node stays empty (v1 syncs the reference only)` を出す。以後の同期は普通に続く。

Principled BSDF の Base Color / Metallic / Roughness / Alpha / Normal / Emission Color に繋いだ画像は v0.7 と同じく `img` で本体が届く。v0.11 では `_image_node()` が「画像未設定の Image Texture ノード」も拾うので、`nt` が先にノードを作り、後から届いた画像がそのノードに入る（ノードの二重生成はしない）。

### 同期しないもの（Non-goal）

Node Group（`ShaderNodeGroup`）の中身と存在、OSL Script ノード、Custom Node / 外部 Addon ノード、Frame（`NodeFrame`）とその親子関係、ノードの幅 / 色 / 折り畳み、Curve 系ノードのカーブ形状（RGB Curves / Vector Curves の `mapping`）、Image Texture の `image_user`（フレーム設定）、Texture Mapping / Color Mapping パネル、画像のピクセル、Geometry Nodes / Compositor / World Shader。
対象外ノードは**両側とも触らない**: 送信側はその名前を `x` に載せるだけ、受信側は自分のローカルにある対象外ノードとそのリンクを残す。対象外ノードに繋がるリンクは相手側では張れない（片端が無い）。

## 同期ループを起こさない仕組み

v0.7 の仕組みそのまま。送信 / 適用のたびにローカル状態（`nt` 込み）の digest を記録し、変わらない限り送らない。適用は `_set()` で「違う時だけ書く」。変更検出は depsgraph の `Material` / `ShaderNodeTree` 更新 ＋ 2 秒ごとの sweep、最大 5Hz。
テストで「何もしない 5 秒間に `mat` / `mat_ren` / `mat_del` / `obj_mats` / `img` が 1 通も出ない」ことを両側で確認。

## 後方互換

| 組み合わせ | 動き |
| --- | --- |
| v0.11 ↔ v0.11 | Node Tree 全体が同期 |
| v0.11 → v0.10 以前 | `nt` は無視される。Principled の値 / テクスチャ / スロットは従来どおり届く |
| v0.10 以前 → v0.11 | `nt` が無いので Principled 経路だけ適用。v0.11 側のツリーは（その Principled 入力以外）触らない |
| 相手が v0.7 未満 | 従来どおり System Console に WARNING（文言に node tree sync v0.11+ を追加） |

## テスト

```
python3 tests/test_material_nodes.py direct      # relay / ws / wss も可。最初に unit（単一プロセス）部分を実行
blender --background --factory-startup --python tests/blender_runner.py -- tests/test_material_nodes.py direct
```

自動テストの内容: プロシージャル構成（TexCoord → Mapping → Noise(4D) → ColorRamp → Base Color、Noise → Bump → Normal）のホスト→ゲスト、
ゲスト側で Mix Shader + Emission を Material Output に差し込み・ノード移動・ColorRamp 要素の色変更 → ホスト、ホスト側でノード削除・ColorRamp 補間変更・Voronoi / Wave / Glass / Transparent / Mix Shader 追加と Mix の差し替え、
Image Texture の参照（パスが存在 → ロードされ interpolation / extension も一致、存在しない → ノードは空のまま）、Node Group が相手に作られない・自分の Node Group が相手の更新で消えない、
同じノードの別入力の同時編集、アイドル時に無通信。unit: round trip、二回目の適用が no-op、差分の中身、未知のノード種別のスキップ、同名別種別の作り直し、v0.10 以前 payload の適用。

確認済み: **bpy 5.0.1（headless）** で direct / relay / ws PASS。既存テスト（test_sync / test_data / test_grease_pencil / test_glb / test_pose / test_armature / test_skinning / test_materials）も PASS。
**Blender 5.2 実機では未確認**。

### 実機（Blender 5.2）での確認手順

1. 両 PC の `scripts/addons/freebird_collab` を v0.11.0 に入れ替え（System Console に `peer ... runs add-on v0.11.0` が出ること）
2. Create Room → Join Room。Cube のマテリアルを Shader Editor で開く
3. A 側: Noise Texture を追加して Base Color に繋ぐ → B 側の Shader Editor に同じノードが同じ位置に出て、Material Preview で模様が出る
4. B 側: ColorRamp を Noise と Base Color の間に挟む → Noise の Scale を変える → ノードを動かす → A 側で追従
5. A 側: Mix Shader + Emission を Material Output に差し込む → B 側で発光する。Emission の Strength を B 側で変える → A 側で追従
6. A 側: Image Texture を追加して Open で画像を開き Emission Color に繋ぐ → B 側: 同じパスに画像があればロード、無ければノードが空で Console に `is not available here` が 1 回出る（v1 仕様）
7. どちらかが Node Group を追加 → 相手には出ない。その後の他の編集は普通に届く
8. 5 秒放置して System Console に `sent mat` が出続けないこと。従来の Transform / Edit Mode / Pose / Skinning が動くこと
