# セッション中の GLB Import 同期（v0.5）

## 症状と原因

実機：接続中に GLB を Import すると相手に出ない（HOST 側は再接続で見える、GUEST 側は持ち込む手段なし）。

v0.4 の新規オブジェクト検知そのもの（`bpy.data.objects` と前回の集合の差分を 30Hz で比較）は GLB Import でも動く（headless で再現テスト済み）。実機で出なかった原因として潰したのは次の 4 点。どれが当たっていたかは実機ログで確定できないので、全部塞いだ。

1. **1 個のオブジェクトの例外が同期ループ全体を止める**：`_sync_objects` はオブジェクトを順に処理していて、途中で例外が出るとその tick は残り全部が送られず、`known` も更新されない → 次の tick でまた同じ所で止まる。Import 品にサポート外の要素（巨大メッシュ、Armature 等）が混じると連鎖的に何も出ない。→ **オブジェクト単位で try/except**、失敗はその名前だけログ（1 回）
2. **名前の squatting**：受信側で同名の datablock が既に `bpy.data.objects` に居ると obj_add を無視していた。ローカルで削除した残骸（orphan）や以前の Import 品が居ると、相手の GLB は永久に出ない。→ シーンに無い同名 ID は `.orphan` に退避、シーンに居るなら **upsert**（データと Transform を更新）
3. **送信側も orphan を拾っていた**：`bpy.data.objects` にはシーンに無い ID も含まれる → 相手にゴーストが出る/名前衝突。→ **現在のシーンに居るオブジェクトだけ**を同期対象に
4. **頂点数上限**：GLB のメッシュは面ごとに頂点が分割されるので大きくなりがち。10 万頂点超は placeholder Empty になっていた。→ 追加時の上限を **30 万頂点**に（Edit Mode のライブ再送は従来通り 10 万まで。超えると形は出るが編集は同期されない、ログに出す）

加えて GLB に必要な機能を追加：

- **parent/child 階層**：obj_add に `parent` を載せ、受信側で親子を復元（親が後着でも保留→解決）。親を動かすと子の world 行列も更新される
- **マテリアル**：スロット名と Base Color（Principled）を obj_add に載せ、受信側で同名マテリアルを作成/再利用して割当。**ノード構成・テクスチャ・画像は送らない**（未対応）
- 送信ログ：`[collab] sent obj_add NAME (MESH, 64 KB, parent X)` が System Console に出るので、実機で「送っているのに出ない」か「そもそも送っていない」かを切り分けられる

## 未対応（明示）

- テクスチャ / 画像 / ノードマテリアル（色だけ）
- Armature / スキニング / アニメーション（Armature は placeholder Empty、Mesh は静止形状で出る）
- 30 万頂点超のメッシュ（placeholder）、10 万〜30 万頂点のメッシュの Edit Mode ライブ同期
- UV / 法線 / 頂点グループ / シェイプキー / モディファイア
- Import 先のコレクション構造（受信側はシーン直下に入る。階層は parent で保持）

## テスト `tests/test_glb.py`（direct / tcp relay / ws / wss / Cloudflare DO relay すべて PASS）

1. HOST が接続中に GLB A（Empty > Cube(赤), Sphere > Cone）を Import → GUEST に 4 オブジェクト出現
2. GUEST が接続中に GLB B を Import → HOST に出現
3. 再接続なし
4. 複数 Mesh（3 個）
5. parent/child 2 段階層が保持され、子の world 位置が正しい
6. Import 後に HOST が Root を移動 → GUEST 側で孫 Tip の world 位置まで追従。GUEST が Import 品の子を移動 → HOST に反映
7. Import 後に Edit Mode で頂点編集 → 相手に反映
8. 追加：マテリアル色（赤）が相手に付く、orphan 同名 ID が居ても出現する、アイドル時の送信ゼロ

回帰：`test_sync.py` / `test_data.py`（direct / wss）PASS、アドオン register/unregister OK。
