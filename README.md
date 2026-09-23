# FreebirdCollabo — Real-time collaboration for Blender (Public Alpha)

Real-time multi-user collaboration for Blender.
Works with or without VR.

FreebirdCollabo is a Blender add-on that synchronizes one Blender scene across multiple Blender instances in real time.

On desktop, it works as a remote co-editing layer for Blender.
Pair it with Freebird XR and the same workflow can be used from inside VR.

Freebird XR is optional.
You can use FreebirdCollabo without a VR headset.

Gravity Sketch の Co-Creation のように、「同じ3D空間を一緒につくる」体験を Blender でも成立させることを目指している公開α版です。

- 本体は独立した小さな Blender アドオン `freebird_collab`（Freebird XR 本体は改変しない）
- ホストの Blender シーンが正本（authoritative）。JOIN 時に 1 回だけ .blend を送り、以後は差分のみ同期
- 1 つのルームに複数人が参加でき、全員の変更が双方向に同期される
- 相手の **選択オブジェクト / 使用中ツール** をデスクトップ 3D ビューに表示。Freebird XR 使用時は **頭（HMD）/ 左右の手 / ポインターのレイ** も VR ビューとデスクトップの両方に描画
- UI は `COLLAB` パネル（N パネル）の **Create Room / Join Room / Leave Room**。Freebird XR 使用時は VR メニューからも操作できる

> ⚠️ Public Alpha です。重要なデータは必ずバックアップを取ってから使ってください。

## 同期している機能

### main ブランチ（アドオン v0.6.2）

| 分類 | 内容 |
| --- | --- |
| オブジェクト | Transform（変更分のみ 30Hz）、新規作成 / 削除、Duplicate、Rename（名前で同一性を追うため、相手側では削除＋追加として反映）、parent / child 階層 |
| メッシュ | Mesh Edit（頂点 / 面。Edit Mode 中もリアルタイム）、UV |
| その他のデータ | Curve、Text、Light、Camera、Empty の内容変更（変更があった物だけ、最大 5Hz、サイズ連動の帯域制限あり） |
| Grease Pencil | 新規作成 / 描画 / 編集（レイヤー・フレーム・ストローク・点・ソリッド材質） |
| マテリアル（基本） | マテリアルのスロット名、Base Color 値、Base Color Texture（同一画像はセッション中 1 回だけ送信） |
| Import | 接続中の GLB Import（階層・複数 Mesh・UV・Base Color テクスチャ付き） |
| Presence | 選択、Active Tool（Freebird があれば `fb:draw.stroke` 等 / なければ Blender のツール）、Freebird XR 使用時の HMD / 左右コントローラー（20Hz）/ ポインターのレイ |

### 開発ブランチで実装済み（実機確認待ち・main 未反映）

以下は Issue ごとのブランチで実装・自動テスト済みで、実機確認のあと main に取り込む予定です。まとめて試す場合は最新の `issue-7-material-node-sync` ブランチ（v0.11.0）を使ってください。詳細はそのブランチの `docs/` を参照。

| 分類 | 内容 | ブランチ |
| --- | --- | --- |
| Materials | 新規作成 / 削除 / Rename、割り当て・スロット、Principled BSDF の主要入力、各入力に繋いだ Image Texture | issue-2-material-sync |
| Pose | Pose Mode の Pose Bone Transform（Location / Rotation / Scale / Rotation Mode） | issue-4-pose-sync |
| Armature / Bone | 接続中の Armature 新規作成、Bone の追加 / 削除 / Rename、head / tail / roll、parent / connected | issue-5-armature-sync |
| Skinning | Vertex Groups、各頂点の Weight、Armature Modifier、相手側でのメッシュ変形 | issue-6-skinning-sync |
| Material Node Tree | Blender 標準 Shader Node のノード追加 / 削除・値・リンク・Material Output 接続 | issue-7-material-node-sync |

## 既知の制限

- Collection 間の移動は未対応
- Undo は共有されない（各自の Undo は自分の Blender だけに効く）
- 権限管理なし（ルームコードを知っている人は誰でも参加・編集できる）
- 音声は内蔵していない（Discord などを併用）
- main では Base Color 以外のマテリアル設定・Armature / Pose / Skinning は同期しない（上記の開発ブランチで対応中）
- 開発ブランチの Armature / Bone の一部の新規作成は、相手側に即時反映されず再接続が必要な場合がある
- Material Node Tree など新しい同期機能はまだ alpha 品質
- 同期対象外：Keyframe / Action / NLA、Geometry Nodes、法線、モディファイア（開発ブランチでは Armature Modifier のみ対応）
- 大人数での利用はまだ最適化していない
- フルデータ同期が必要な場合は Multiuser 0.8.x と併用する設計余地あり（docs/research.md）

## 構成

```
FreebirdCollabo/
├─ freebird_collab/          Blender アドオン本体（scripts/addons にフォルダごと置く）
│   ├─ __init__.py           UI・オペレーター・タイマー・公開API
│   ├─ session.py            同期ロジック（正本共有 / Transform / オブジェクトデータ / Presence）
│   ├─ object_data.py        Mesh/Curve/Grease Pencil/Text/Light/Camera/Empty の内容をシリアライズ・in-place 適用
│   ├─ presence.py           相手の頭・手・レイ・選択枠・ラベルの GPU 描画
│   ├─ hub.py                ROOM ハブ（relay サーバー兼 direct モードのホスト内蔵サーバー）
│   ├─ link.py               TCP クライアント（受信スレッド → メインスレッドへキュー）
│   ├─ protocol.py           メッセージ定義（4byte 長 + JSON）
│   └─ ws.py                 WebSocket 実装（stdlib のみ）— 無料 HTTP ホスティングに Relay を置くため
├─ freebird_plugin/
│   └─ freebird_collab_menu.py   Freebird VR メニューに COLLAB ボタンを足すプラグイン（Freebird XR 使用時のみ）
├─ relay/collab_relay.py     中継サーバー（依存なし・TCP と WebSocket を同一ポートで自動判別）
├─ relay/cloudflare/         Cloudflare Workers + Durable Objects 版 Relay（無料・固定URL・常時起動）
├─ relay/quick_tunnel.bat/.py  ホスト PC で relay + cloudflared quick tunnel を 1 発起動（アカウント不要）
├─ relay/relay_check.py      Relay 到達確認 CLI
├─ tests/test_sync.py        Blender 2 インスタンス自動同期テスト（VR不要）
├─ tests/test_data.py        オブジェクトデータ同期テスト（Edit Mode 頂点編集・全型・削除・送信量）
├─ tests/test_grease_pencil.py  Grease Pencil同期テスト（新規作成・描画・編集・材質・双方向）
├─ tests/test_glb.py         接続中の GLB Import 同期テスト（双方向・階層・複数 Mesh・Import 後の編集）
├─ tests/test_glb_real.py    実 GLB ファイルを使った Import 同期テスト（COLLAB_GLBS でパス指定）
├─ docs/research.md          調査メモ・アーキテクチャ・リスク
├─ docs/internet-relay.md    インターネット越し ROOM コード参加の公開手順・実機テスト手順
├─ docs/machida-merge-v0.4.md 外部からの修正提案の統合レビュー（採用/不採用/理由）・同期対象一覧
├─ docs/glb-import-v0.5.md   接続中の GLB Import 同期（原因・階層/マテリアル対応・未対応事項）
├─ docs/textures-v0.6.md     Base Color テクスチャ＋UV の転送（画像の再送なし・未対応事項）
└─ docs/textures-v0.6.1-sanze.md 実 GLB 2 種でのテクスチャ転送比較調査
```

## セットアップ（参加する全員の PC で同じ）

1. `freebird_collab` フォルダを `C:\Users\<name>\AppData\Roaming\Blender Foundation\Blender\5.2\scripts\addons\` にコピー → Preferences > Add-ons で **Freebird Collaboration Layer** を有効化
2. アドオン設定で **Display Name / My Color / Connection** を設定
   - **Relay server (room code)**（推奨）: Relay URL に `wss://...`（`docs/internet-relay.md` の手順で作った固定 URL）を入れ、**Check Relay** で OK を確認（設定は 1 回だけ）。LAN 内なら `ws://192.168.x.x:7788` で `python3 relay/collab_relay.py` を動かした PC でも可
   - **Direct (IP address)**: ホストがポート 7788 で待ち受け。ゲストは `Host IP` 欄に IP（Tailscale 等の VPN や LAN）を入力。デバッグ用
3. （Freebird XR を使う場合のみ）`freebird_plugin/freebird_collab_menu.py` を `C:\Users\<name>\.freebird\plugins\` にコピー → Freebird Settings で Reload All（VR メニューの CUSTOM に Create/Join/Leave Room が出る）

## 使い方

| 手順 | ホスト | ゲスト |
| --- | --- | --- |
| 1 | 正本にしたい .blend を開き COLLAB パネル（または VR メニュー）で **Create Room** | |
| 2 | パネルに出る **6 文字のルームコード**を Discord などで伝える | コードを COLLAB パネルの Code 欄に入れて **Join Room**（VR メニューの Join Room は、この欄のコードで参加） |
| 3 | | ホストのシーンが自分の Blender に読み込まれる |
| 4 | お互いの編集がリアルタイムに反映される。相手の選択枠・「名前 / ツール / Selected: Cube」ラベルが見える。Freebird XR を起動していれば相手の頭（ワイヤーキューブ）・手（ピラミッド）・レイも見える | |
| 5 | **Save Master Scene** で正本を保存（ゲストが押すとホスト側で保存される） | |
| 6 | **Leave Room** | **Leave Room** |

音声は Discord などを使う（内蔵しない）。

## テスト

```
pip install bpy==5.0.1          # Blender を pip の bpy モジュールとして使う
python3 tests/test_sync.py direct   # LAN direct
python3 tests/test_sync.py relay    # tcp relay
python3 tests/test_sync.py ws       # websocket relay
python3 tests/test_sync.py wss      # websocket over TLS (local self-signed terminator)
python3 tests/test_data.py direct   # object data sync (same modes as above)
python3 tests/test_grease_pencil.py direct  # Grease Pencil create/draw/edit sync
python3 tests/test_glb.py direct    # GLB import during a session (same modes as above)
```

ホスト / ゲスト 2 プロセス（+ relay）を起動し、Create → Join → 正本共有 → Cube 移動の双方向同期 → 新規オブジェクト → 選択 / ツール presence → ホスト保存 までを自動検証する（PASS 済み）。VR ヘッドセットは不要。開発ブランチの各機能のテストは、そのブランチの `tests/` と README を参照。
