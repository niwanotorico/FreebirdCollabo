# sanze.glb / zamapoly.glb 比較調査（v0.6.1）

## 2 ファイルの違い（GLB の中身）

| | sanze.glb | zamapoly.glb |
| --- | --- | --- |
| サイズ | 7.5 MB | 28.6 MB |
| Mesh | 1 個 / 28,889 頂点 | 1 個 / 125,354 頂点（10 マテリアル） |
| 頂点属性 | POSITION, **NORMAL**, TEXCOORD_0 | POSITION, TEXCOORD_0 |
| マテリアル | Base Color + **Normal + Occlusion**（画像 3 枚, jpeg 4096²） | Base Color のみ（画像 10 枚, jpeg 最大 8192²） |
| Blender 取り込み後の Base Color チェーン（bpy 5.0.1） | Principled ← Image Texture 直結 | 同左 |

## 再現テスト（`tests/test_glb_real.py`、実ファイルで実行）

両ファイルを使って、以下をすべて **headless（bpy 5.0.1）で PASS**：

1. GUEST が sanze 単体を Import → HOST にテクスチャ付きで出る
2. GUEST が zama → sanze の順（同一セッション）→ 両方 OK（画像は 11 枚、再送なし）
3. HOST が zama、GUEST が sanze → OK
4. HOST がルーム作成前に sanze を Import→削除（画像だけ残留）→ 接続後に再 Import → GUEST に OK
5. 上記を wss（TLS）と ws relay 経由でも OK

つまり **Normal / Occlusion の有無で送信処理は分岐しない**。現在の bpy 5.0.1 では両ファイルとも同じ経路で成功する。

## それでも実機で sanze が失敗しうる箇所（コードを読んで潰したもの）

1. **スナップショット重複排除の誤判定（ホスト送信時）**  
   v0.6 は「スナップショット送信時点で pack 済みだった画像」を全部「ゲストが持っている」と見なして送らなかった。しかし 0 ユーザーの画像（前に Import して削除した残骸）は `.blend` に**保存されない** → ゲストは持っていないのに送られない → テクスチャ無し。  
   ホストが一度 sanze を試して削除 → ルーム作成 → 再 Import、という実機の流れで起きる。zama は初回だったので当たらない。  
   → v0.6.1 は保存した `.blend` に実際に入った画像名（`bpy.data.libraries.load`）だけを索引にする
2. **欠損時に復旧手段が無かった**  
   → 受信側は obj_add の `tex.id` が手元に無ければ `img_need` を送信元へ送り、送信元は内容ハッシュで画像を探して `img` を返す。到着後にマテリアルへ接続。テストで両方向を確認（`COLLAB_GLB_NO_PUSH=1` で送信側の画像プッシュを止めて復旧経路だけで通す）
3. **Base Color チェーンの探索が浅かった**  
   Blender 5.2 の glTF Importer が Mix（頂点カラー × テクスチャ）等を挟む場合、最初の入力しか辿らず画像を見失う可能性 → 幅優先で全入力を辿る（Mix / Gamma / Group 経由も可）。単体テスト済み

## 実機での確認方法

System Console の `[collab]` 行を見る。v0.6.1 では次のいずれかが必ず出る：

- `sent image NAME (xx KB, id)` … 送信側がプッシュした
- `texture xxxxxxxx for OBJ not here yet, requested from uN` → `re-sent image ... on request` → `received image ...` … 復旧経路
- `img_need xxxxxxxx: image not found here` … 送信側にも画像が無い（pack されていない／ファイルが無い／24 MB 超）→ このログを送ってください

## 実行方法

```
COLLAB_GLBS=C:\path\sanze.glb,C:\path\zamapoly.glb python tests\test_glb_real.py direct
COLLAB_GLB_SIDES=host,guest      # 誰が Import するか（省略時 guest）
COLLAB_GLB_PRE_HOST=...sanze.glb # ホストが事前に Import→削除しておく
COLLAB_GLB_NO_PUSH=1             # 画像プッシュを止めて img_need 復旧だけで通す
```
