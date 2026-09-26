# VR Studio v0.2 — Look（質感 8 種）＋ 右側「VR Studio 拡張エリア」

v0.1（[vr-studio-v0.1.md](vr-studio-v0.1.md)）の 32 色 Color は実機確認済み。v0.2 で追加・変更したもの：

1. **Look パレット**：Clay / Matte / Glossy / Plastic / Metallic / Glass / Emission / Toon。「選択 → Look を押す → 即変わる」
2. **Color と Look は独立**：青 + Matte → 青 + Metallic → 青 + Glass（色はそのまま）、Glass のまま色を変えても Glass のまま
3. **配置を右側へ**：Freebird メイン メニューの右隣に「VR Studio 拡張エリア」。手に追従する挙動はそのまま
4. Freebird 本体・FreebirdCollabo 本体は **無変更**（v0.1 と同じ）

## 1. VR Studio 拡張エリア（`area.py`）

- メイン メニューの**右隣・下端揃え**（右利き。左利き設定なら左右反転＝Freebird のサブメニューと同じ側）
- 毎フレーム左手コントローラーから位置を計算 → 手と一緒に動く。ワールドを拡大縮小しても実寸のまま（`fixed_scale`）
- Freebird 自身のサブメニュー（Pen / Shape / Edit / Plugins / Mirror）が右側に開いて重なる時だけ、その外側へスッと**スライド**して避け、閉じたら戻る（Select ツールのオプションは左側に出るので、選択→色/質感の流れではメニューの真横に居る）
- Freebird のメニュー配置は `freebird.ui.main_menu` の実物の寸法を毎フレーム読む（読めない時は v2.15 の寸法で代用）
- エリアは「セクション」の縦積み。表示中のセクションだけ下から順に積む（Color が下、Look がその上）
- セクション追加 = `StudioSection` を継承したクラス 1 つ + `__init__.py` の `SECTIONS` に 1 行（ランチャーボタンも自動で増える）→ Assets / Modifiers / Favorites を同じ形で足していける
- 12 フレームごとに「今選んでいるオブジェクトの色と Look」を読んで、該当スウォッチ／タイルに青枠（相手が Collab で変えた場合も追従）

```
freebird_plugin/vr_studio/
  __init__.py        fb_info, SECTIONS（Color / Look）, ランチャーボタン, VR 開始時にエリアを組む
  area.py            StudioArea（配置・スライド・縦積み）, StudioSection（パネル共通）, Tile（押せるタイル）
  color_section.py   COLOR（32 色）
  look_section.py    LOOK（8 種、サムネ + 名前）
  palette.py         32 色データ
  looks.py           8 Look の値（データのみ）
  materials.py       Blender データへの書き込み（Color / Look / Look 判定）。bpy のみ
  icons/             color.png, look.png, look_<name>.png（tools/make_vr_studio_icons.py で生成）
```

## 2. Look の設計

- **Look が持つもの**＝Principled BSDF の「色以外」の入力一式（`looks.LOOK_INPUTS`）＋ マテリアル設定 3 つ（Render Method / Raytraced Transmission / Solid 表示の透明度）。**Base Color は Color の持ち物で Look は触らない**
- Look は毎回その一式を**全部**書く（既定値 + その Look の上書き）→ Glass → Matte で透過が残る、のような「前の Look の残りカス」が出ない
- 色に依存する部分は**マテリアル自身から判断**（カスタムプロパティに頼らない）ので、Collab で相手が押しても同じ動きになる
  - Emission：Emission Strength > 0 なら、Color は Emission Color にも同じ色を書く
  - Toon：`VR Toon Tint` ノードがあれば、Color はその色も書く
- 今の Look は値の一致で判定（`detect_look`）→ パネルの青枠
- テクスチャやノードが刺さっている入力（例：Roughness マップ）は上書きしない
- Principled の無いマテリアル（Emission だけ等）に Look を押すと、その色を引き継いだ Principled を作って差し替える（古いノードはツリーに残る。Undo で戻る）
- マテリアル無し → 新規、他の未選択オブジェクトと共有 → コピーしてから（Color と同じルール）
- Grease Pencil は Look 対象外（色のみ）

### 8 種の値（Blender 5.x Principled BSDF / AgX / 既定値からの差分）

| Look | Principled（既定値からの変更） | その他 | 狙い |
|---|---|---|---|
| Clay | Roughness 1.0, Diffuse Roughness 1.0, Specular IOR Level 0.15, Sheen 0.2 / Sheen Roughness 0.8 | | 粉っぽい造形用粘土。ハイライトほぼ無し、縁がふわっと明るい |
| Matte | Roughness 0.8, Diffuse Roughness 0.3, Specular IOR Level 0.35 | | つや消し塗装 |
| Glossy | Roughness 0.12, Coat 1.0 / Coat Roughness 0.02 | | クリア塗装・キャンディ。下地の上に鋭い反射 |
| Plastic | Roughness 0.35, IOR 1.46 | | 成形プラ（ABS）の半ツヤ |
| Metallic | Metallic 1.0, Roughness 0.25 | | 色付き金属（色＝金属の色）。少しだけ荒らして映り込みを柔らかく |
| Glass | Transmission 1.0, Roughness 0, IOR 1.45 | Raytraced Transmission ON, Render Method Dithered, Solid 表示の透明度 0.35 | 色付きガラス |
| Emission | Emission Strength 2.0（色 = Base Color） | | 自発光。AgX で白飛びしにくい強さ |
| Toon | Roughness 0.8, Specular 0.35（Principled は色の保持用） | Diffuse → Shader to RGB → ColorRamp（Constant 3 段）→ Mix Multiply（色）→ Emission → Output | セル調 3 階調 |

既定値：Metallic 0 / Roughness 0.5 / IOR 1.5 / Specular IOR Level 0.5 / Coat 0（Coat Roughness 0.03）/ Sheen 0 / Transmission 0 / Emission Strength 0 / Diffuse Roughness 0（Blender 5.0 の Principled の既定値を実測）。
Solid 表示にも効くように `Material.metallic / roughness`（ビューポート表示）も同じ値にしている。

### エンジンごとの注意

- **Glass**：EEVEE（4.2 以降）で後ろの物体が屈折して透けるには、シーンの Render Settings > Raytracing が ON である必要がある（マテリアル側の Raytraced Transmission は Look が ON にする）。OFF だとワールド（HDRI）だけが透けて見える。シーン設定は共同編集の相手にも影響するので VR Studio は触らない。Solid 表示では半透明（0.35）で表示
- **Toon**：Shader to RGB は EEVEE 専用。Cycles では Toon だけ平らな色になる。Solid 表示では普通の色
- **Emission**：Solid 表示では光らない（Material Preview / Rendered で光る）

参考：[EEVEE 4.2 migration](https://developer.blender.org/docs/release_notes/4.2/eevee_migration/)（Blended では屈折不可、Dithered + Raytracing を使う）

## 3. 共同編集

VR Studio は Collab を import しない。Look で変わる Principled の値・マテリアル設定（Render Method）・Toon のノード（Diffuse / Shader to RGB / ColorRamp / Mix / Emission）とリンクは、
**既存の v0.7 Material 同期 + v0.11 Node Tree 同期でそのまま相手に届く**（テストで確認：Toon と Emission を片方で付けて相手に届く → 相手が Glass + 別の色に変えて戻ってくる、Toon のノードも消える）。
Raytraced Transmission と Solid 表示の透明度は同期対象外（見た目への影響は小さい）。

## 4. 動作確認

```
python3 tests/test_vr_studio.py unit     # Color + Look（8 種の判定、独立性、Glass→Matte で残らない、Emission の色追従、Toon ノード、テクスチャ優先、非 Principled、copy-on-write）
FREEBIRD_XR_DIR=".../scripts/addons/freebird_xr" python3 tests/test_vr_studio.py ui
                                         # Freebird の本物の main_menu を読み込んで、右隣・下端揃え・Pen サブメニューを避ける・Select では動かない・左利きで反転、
                                         # レーザーと同じ raycast でスウォッチ／Look タイルに当たる、Color→Look→Color で互いに保たれる、青枠
python3 tests/test_vr_studio.py direct   # 2 インスタンス：Color / Look（Toon・Emission・Glass）が既存同期で両方向に届く
python3 tests/test_vr_studio.py relay
```

確認済み：**bpy 5.0.1（headless）** で unit / ui（Freebird v2.15.0 の bl_xr と main_menu を使用）/ direct / relay すべて PASS。
未確認：Blender 5.2 + Freebird 実機での見た目（Look の質感の見え方、エリアの位置、スライドの動き）。

### 実機での確認手順

1. Blender を再起動（プラグインは起動時に読まれる。`~/.freebird/plugins/vr_studio` は更新済み）
2. VR 開始 → 左手 B（メニュー）→ Plugins → **Color** と **Look**（それぞれ表示／非表示）
3. メイン メニューの右隣に COLOR、その上に LOOK が出ること。Pen ツールのオプションが開いている時はその外側に避けること
4. Select ツールで選択 → 青 → Matte → Metallic → Glass（色が青のまま）→ 赤（Glass のまま）
5. Material Preview で質感を見る（Glass の透けは Raytracing ON の時）
6. Collab で相手側にも同じ見た目が出ること
