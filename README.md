# Freebird Collaboration Layer (MVP v0.11.0)

Blender + Freebird XR で、Gravity Sketch の Co-Creation に近い「遠隔VR共同編集」を成立させる最小プロトタイプ。

- 独立した小さな Blender アドオン `freebird_collab`（Freebird XR 本体は改変しない）
- ホストの Blender シーンが正本（authoritative）。JOIN 時に 1 回だけ .blend を送り、以後は差分のみ同期
- 相手の **頭 / 左右の手 / ポインターのレイ / 選択オブジェクト / 使用中ツール** をデスクトップ 3D ビューと VR ビューの両方に描画
- UI は `COLLAB` パネル（N パネル）と Freebird VR メニューの **Create Room / Join Room / Leave Room** だけ

```
FreebirdCollabo/
├─ freebird_collab/          Blender アドオン（scripts/addons にフォルダごと置く）
│   ├─ __init__.py           UI・オペレーター・タイマー・公開API
│   ├─ session.py            同期ロジック（正本共有 / Transform / オブジェクトデータ / Presence）
│   ├─ object_data.py        Mesh/Curve/Grease Pencil/Text/Light/Camera/Empty/Armature の内容をシリアライズ・in-place 適用
│   ├─ presence.py           相手の頭・手・レイ・選択枠・ラベルの GPU 描画
│   ├─ hub.py                ROOM ハブ（relay サーバー兼 direct モードのホスト内蔵サーバー）
│   ├─ link.py               TCP クライアント（受信スレッド → メインスレッドへキュー）
│   ├─ protocol.py           メッセージ定義（4byte 長 + JSON）
│   └─ ws.py                 WebSocket 実装（stdlib のみ）— 無料 HTTP ホスティングに Relay を置くため
├─ freebird_plugin/
│   └─ freebird_collab_menu.py   Freebird VR メニューに COLLAB ボタンを足すプラグイン
├─ relay/collab_relay.py     中継サーバー（依存なし・TCP と WebSocket を同一ポートで自動判別）
├─ relay/cloudflare/         Cloudflare Workers + Durable Objects 版 Relay（無料・固定URL・常時起動）
├─ relay/quick_tunnel.bat/.py  3090 PC で relay + cloudflared quick tunnel を 1 発起動（アカウント不要）
├─ relay/relay_check.py      Relay 到達確認 CLI
├─ tests/test_sync.py        Blender 2 インスタンス自動同期テスト（VR不要）
├─ tests/test_data.py        オブジェクトデータ同期テスト（Edit Mode 頂点編集・全型・削除・送信量）
├─ tests/test_grease_pencil.py  Grease Pencil同期テスト（新規作成・描画・編集・材質・双方向）
├─ tests/test_materials.py   マテリアル同期テスト（作成/削除/Rename・スロット・Principled値・画像テクスチャ・再接続・同時編集・双方向）
├─ tests/test_material_nodes.py  Shader Node Tree 同期テスト（ノード追加/削除/移動・プロパティ・リンク・Mix Shader/Emission/Glass・ColorRamp・Image Texture 参照・Node Group 非対象・同時編集・双方向）
├─ tests/test_pose.py        Pose Mode ボーン Transform 同期テスト（Location/Rotation/Scale 双方向・複数 Bone・接続中に追加した Bone・再接続・無通信）
├─ tests/test_armature.py    Armature / Bone 構造同期テスト（接続中の Armature 新規作成・Bone 追加/削除/Rename・head/tail/roll・parent/connected・Edit Mode 中の保留・再接続・Pose 連携）
├─ tests/test_skinning.py    Vertex Group / Skinning 同期テスト（Automatic Weights・Weight Paint・Group 追加/Rename/削除・Armature Modifier・相手側メッシュの変形一致・Weight Paint 中の保留・再接続）
├─ tests/test_glb.py         接続中の GLB Import 同期テスト（双方向・階層・複数 Mesh・Import 後の編集）
├─ docs/research.md          調査メモ・アーキテクチャ・リスク
├─ docs/internet-relay.md    インターネット越し ROOM コード参加の公開手順・実機テスト手順
├─ docs/machida-merge-v0.4.md 町田修正の統合レビュー（採用/不採用/理由）・同期対象一覧
├─ docs/glb-import-v0.5.md   接続中の GLB Import 同期（原因・階層/マテリアル対応・未対応事項）
├─ docs/textures-v0.6.md     Base Color テクスチャ＋UV の転送（画像の再送なし・未対応事項）
├─ docs/material-sync-v0.7.md マテリアル同期（Issue #2: 同期範囲・メッセージ・ループ防止・実機確認手順）
├─ docs/pose-sync-v0.8.md    Pose Mode ボーン Transform 同期（同期範囲・ループ防止・再接続・実機確認手順）
└─ docs/armature-sync-v0.9.md Armature / Bone 構造同期（Issue #5: 同期範囲・Edit Mode 衝突時の扱い・Pose との順序・実機確認手順）
└─ docs/skinning-sync-v0.10.md Vertex Group / Skinning 同期（Issue #6: 同期範囲・Weight Paint 中の扱い・Armature Modifier の参照解決・実機確認手順）
└─ docs/material-node-sync-v0.11.md Shader Node Tree 同期（Issue #7: payload・差分・Image Texture 参照・対象外ノード・後方互換・実機確認手順）
```

## セットアップ（両 PC で同じ）

1. `freebird_collab` フォルダを `C:\Users\<name>\AppData\Roaming\Blender Foundation\Blender\5.2\scripts\addons\` にコピー → Preferences > Add-ons で **Freebird Collaboration Layer** を有効化
2. アドオン設定で **Display Name / My Color / Connection** を設定
   - **Relay server (room code)**（推奨）: Relay URL に `wss://...`（`docs/internet-relay.md` の手順で作った固定 URL）を入れ、**Check Relay** で OK を確認（設定は 1 回だけ）。LAN 内なら `ws://192.168.x.x:7788` で `python3 relay/collab_relay.py` を動かした PC でも可
   - **Direct (IP address)**: ホストがポート 7788 で待ち受け。ゲストは `Host IP` 欄に IP（Tailscale 等の VPN や LAN）を入力。デバッグ用
3. `freebird_plugin/freebird_collab_menu.py` を `C:\Users\<name>\.freebird\plugins\` にコピー → Freebird Settings で Reload All（VR メニューの CUSTOM に Create/Join/Leave Room が出る）

## 使い方

| 手順 | ホスト | ゲスト |
| --- | --- | --- |
| 1 | 正本にしたい .blend を開き **Create Room**（VR メニュー or COLLAB パネル） | |
| 2 | パネルに出る **6 文字のルームコード**を Discord などで伝える | コードを COLLAB パネルの Code 欄に入れて **Join Room**（VR メニューの Join Room は、この欄のコードで参加） |
| 3 | | ホストのシーンが自分の Blender に読み込まれる |
| 4 | 両者 Freebird XR を起動。相手の頭（ワイヤーキューブ）・手（ピラミッド）・レイ・選択枠・「名前 / ツール / Selected: Cube」ラベルが見える。Cube を動かすと相手にも反映 | |
| 5 | **Save Master Scene** で正本を保存（ゲストが押すとホスト側で保存される） | |
| 6 | **Leave Room** | **Leave Room** |

音声は Discord などを使う（内蔵しない）。

## 同期しているもの / していないもの

同期する: オブジェクトの Transform（変更分のみ 30Hz）、オブジェクトの追加・削除（GLB Import 等で一度に増えた物も。parent/child 階層、UV、マテリアルのスロット名＋Base Color 値＋**Base Color テクスチャ画像**付き。同一画像はセッション中 1 回だけ送信）、**データ内容の変更**（Mesh の頂点/面 — Edit Mode 中もリアルタイム、Curve、Grease Pencil のレイヤー/フレーム/ストローク/点とソリッド材質、Text、Light、Camera、Empty。変更があった物だけ、最大 5Hz、サイズ連動の帯域制限あり）、**マテリアル**（新規作成 / 削除 / Rename、オブジェクトへの割り当て・スロット数・スロットの Link、Principled BSDF の主要入力 — Base Color / Metallic / Roughness / Alpha / IOR / Emission / Coat / Sheen / Transmission ほか、Render Method、Backface Culling、Viewport Display、Base Color / Metallic / Roughness / Alpha / Normal / Emission に繋いだ Image Texture の追加・差し替え・解除。変わった項目だけを最大 5Hz で送信。docs/material-sync-v0.7.md）、**Shader Node Tree**（Blender 標準 Shader Node のノード追加 / 削除・種類・名前・位置・input 値・ノード固有プロパティ・ノード間リンク・Material Output 接続を双方向同期。Principled / Emission / Diffuse / Glass / Transparent / Mix Shader / ColorRamp / Noise / Voronoi / Wave / Mapping / Texture Coordinate / Normal Map / Bump など。Image Texture は参照（名前 / パス / Color Space）のみで画像本体は送らない。変わったノードの変わった値だけを最大 5Hz で送信。v0.10 以前の相手には従来の Principled 同期だけが届く。docs/material-node-sync-v0.11.md）、**Pose Mode のボーン Transform**（Armature の Pose Bone の Location / Rotation / Scale と Rotation Mode をボーン名で対応付けて双方向同期。変わったボーンだけ最大 15Hz。docs/pose-sync-v0.8.md）、**Armature / Bone 構造**（接続中に新規作成した Armature は相手側にも実 Armature として生成。Edit Bone の追加 / 削除 / Rename（名前ベースなので削除＋作成として届く）、head / tail / roll、parent / connected を双方向同期。Edit Mode 中もリアルタイム、最大 5Hz。相手が同じ Armature を Edit Mode 中なら抜けるまで保留、両側が同時に構造を変えた場合は後に Edit Mode を抜けた側が勝つ。docs/armature-sync-v0.9.md）、**Vertex Group / Skinning**（Mesh の Vertex Group の作成 / 削除 / Rename、各頂点の Weight（Weight Paint Mode 中もリアルタイム、Automatic Weights の結果も）、Armature Modifier とその Armature オブジェクト参照・主要設定を Mesh データと一緒に双方向同期。相手側でも Pose に合わせてメッシュが変形する。相手が同じ Mesh を Weight Paint / Edit 中なら抜けるまで保留、両側が同時に変えた場合は後に抜けた側が勝つ。docs/skinning-sync-v0.10.md）、選択、使用中ツール（Freebird があれば `fb:draw.stroke` 等 / なければ Blender のツール）、HMD と左右コントローラーの位置回転（20Hz）、レイ。

同期しない（MVP 非目標）: Shader Node のうち Node Group の中身 / OSL Script / Custom・外部 Addon ノード / Frame / RGB Curves 等のカーブ形状 / Image Texture の画像本体、Geometry Nodes、Compositor、World Shader、Armature の Bone 構造・Skinning 以外のリグ設定（Constraints / IK / Drivers / Bone Envelope の形状 / Custom Shape / Bone Collection の詳細 / Rigify）/ アニメーション（Keyframe / Action / NLA）、法線 / Armature 以外のモディファイア、Undo、3 人以上の最適化、権限管理、音声。フルデータ同期が必要になったら Multiuser 0.8.x と併用する設計余地あり（docs/research.md）。

## テスト

```
pip install bpy==5.0.1          # Blender を pip の bpy モジュールとして使う
python3 tests/test_sync.py direct   # LAN direct
python3 tests/test_sync.py relay    # tcp relay
python3 tests/test_sync.py ws       # websocket relay
python3 tests/test_sync.py wss      # websocket over TLS (local self-signed terminator)
python3 tests/test_data.py direct   # object data sync (same modes as above)
python3 tests/test_grease_pencil.py direct  # Grease Pencil create/draw/edit sync
python3 tests/test_materials.py direct  # material sync (same modes as above; "unit" = single-process part only)
python3 tests/test_material_nodes.py direct  # shader node tree sync (Issue #7)
python3 tests/test_glb.py direct    # GLB import during a session (same modes as above)
python3 tests/test_pose.py direct   # Pose Mode bone transform sync
python3 tests/test_armature.py direct  # Armature / Bone structure sync (Issue #5)
python3 tests/test_skinning.py direct  # Vertex Group / Skinning sync (Issue #6)
```

ホスト / ゲスト 2 プロセス（+ relay）を起動し、Create → Join → 正本共有 → Cube 移動の双方向同期 → 新規オブジェクト → 選択 / ツール presence → ホスト保存 までを自動検証する（PASS 済み）。
