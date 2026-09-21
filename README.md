# Freebird Collaboration Layer (MVP v0.7.0)

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
│   ├─ object_data.py        Mesh/Curve/Grease Pencil/Text/Light/Camera/Empty の内容をシリアライズ・in-place 適用
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
├─ tests/test_glb.py         接続中の GLB Import 同期テスト（双方向・階層・複数 Mesh・Import 後の編集）
├─ docs/research.md          調査メモ・アーキテクチャ・リスク
├─ docs/internet-relay.md    インターネット越し ROOM コード参加の公開手順・実機テスト手順
├─ docs/machida-merge-v0.4.md 町田修正の統合レビュー（採用/不採用/理由）・同期対象一覧
├─ docs/glb-import-v0.5.md   接続中の GLB Import 同期（原因・階層/マテリアル対応・未対応事項）
├─ docs/textures-v0.6.md     Base Color テクスチャ＋UV の転送（画像の再送なし・未対応事項）
└─ docs/material-sync-v0.7.md マテリアル同期（Issue #2: 同期範囲・メッセージ・ループ防止・実機確認手順）
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

同期する: オブジェクトの Transform（変更分のみ 30Hz）、オブジェクトの追加・削除（GLB Import 等で一度に増えた物も。parent/child 階層、UV、マテリアルのスロット名＋Base Color 値＋**Base Color テクスチャ画像**付き。同一画像はセッション中 1 回だけ送信）、**データ内容の変更**（Mesh の頂点/面 — Edit Mode 中もリアルタイム、Curve、Grease Pencil のレイヤー/フレーム/ストローク/点とソリッド材質、Text、Light、Camera、Empty。変更があった物だけ、最大 5Hz、サイズ連動の帯域制限あり）、**マテリアル**（新規作成 / 削除 / Rename、オブジェクトへの割り当て・スロット数・スロットの Link、Principled BSDF の主要入力 — Base Color / Metallic / Roughness / Alpha / IOR / Emission / Coat / Sheen / Transmission ほか、Render Method、Backface Culling、Viewport Display、Base Color / Metallic / Roughness / Alpha / Normal / Emission に繋いだ Image Texture の追加・差し替え・解除。変わった項目だけを最大 5Hz で送信。docs/material-sync-v0.7.md）、選択、使用中ツール（Freebird があれば `fb:draw.stroke` 等 / なければ Blender のツール）、HMD と左右コントローラーの位置回転（20Hz）、レイ。

同期しない（MVP 非目標）: 任意の Shader Node Graph（Principled BSDF 以外のシェーダー、プロシージャルノード、Mapping、Custom Node Group）、Geometry Nodes、Compositor、World Shader、Armature / アニメーション、法線 / モディファイア、Undo、3 人以上の最適化、権限管理、音声。フルデータ同期が必要になったら Multiuser 0.8.x と併用する設計余地あり（docs/research.md）。

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
python3 tests/test_glb.py direct    # GLB import during a session (same modes as above)
```

ホスト / ゲスト 2 プロセス（+ relay）を起動し、Create → Join → 正本共有 → Cube 移動の双方向同期 → 新規オブジェクト → 選択 / ツール presence → ホスト保存 までを自動検証する（PASS 済み）。
