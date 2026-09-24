# FreebirdCollabo

**Blender のためのリアルタイム共同編集アドオン — VR があってもなくても使えます。**

[English README](README.md)

> ⚠️ **Public Alpha（v0.11.1）** — まだ開発初期の公開α版です。重要な `.blend` ファイルは必ずバックアップを取ってから使ってください。

FreebirdCollabo は、1 つの Blender シーンを複数の Blender 間でリアルタイムに同期する Blender アドオンです。

- 🖥️ **VR ヘッドセットは不要。** 普通のデスクトップ環境で、Blender の遠隔共同編集レイヤーとして使えます。
- 🥽 **VR は任意。** Freebird XR と組み合わせれば、同じワークフローを VR の中からも使えます。

Gravity Sketch の Co-Creation のように、「同じ3D空間を一緒につくる」体験を Blender でも成立させることを目指しています。

## はじめかた（3 ステップ）

> **接続まわりの設定は不要です。**
> Relay URL は最初から設定済みなので、**Relay URL** や **Check Relay** は触らなくて大丈夫。
> **同じ Room Code** を入力して Join するだけでつながります。

1. **全員**：[Releases](https://github.com/niwanotorico/FreebirdCollabo/releases) ページから `freebird_collab_addon.zip` をダウンロード。Blender の **Edit > Preferences > Add-ons > Install from Disk** でその zip を選び、**Freebird Collaboration Layer** にチェックを入れる
2. **ホスト（部屋を作る人）**：3D ビューで `N` キー → **COLLAB** タブ → **Create Room**。表示された **6 文字の Room Code** を Discord などで伝える
3. **参加する人**：同じく **COLLAB** タブの **Code** 欄に、教えてもらった Room Code を入力 → **Join Room**

ホストのシーンが読み込まれたら接続完了です。あとはお互いの編集がリアルタイムに反映されます。

## 概要

- 本体は独立した小さな Blender アドオン `freebird_collab`（Freebird XR 本体は改変しない）
- ホストの Blender シーンが正本（authoritative）。JOIN 時に 1 回だけ .blend を送り、以後は差分のみ同期
- 1 つのルームに複数人が参加でき、全員の変更が双方向に同期される
- 相手の **選択オブジェクト / 使用中ツール** をデスクトップ 3D ビューに表示。Freebird XR 使用時は **頭（HMD）/ 左右の手 / ポインターのレイ** も VR ビューとデスクトップの両方に描画
- UI は `COLLAB` パネル（N パネル）の **Create Room / Join Room / Leave Room** だけ。Freebird XR 使用時は VR メニューからも操作できる

## 同期している機能（v0.11.1）

すべて Blender 5.2 の実機で同期を確認済みです。

| 分類 | 内容 |
| --- | --- |
| オブジェクト | Transform（変更分のみ 30Hz）、新規作成 / 削除、Duplicate、Rename、parent / child 階層 |
| メッシュ | Mesh Edit（頂点 / 面。Edit Mode 中もリアルタイム）、UV |
| その他のデータ | Curve、Text、Light、Camera、Empty の内容変更（変更があった物だけ、最大 5Hz、サイズ連動の帯域制限あり） |
| Grease Pencil | 新規作成 / 描画 / 編集（レイヤー・フレーム・ストローク・点・ソリッド材質） |
| マテリアル | 新規作成 / 削除 / Rename、オブジェクトへの割り当て・スロット、Principled BSDF の主要入力（Base Color / Metallic / Roughness / Alpha / IOR / Emission / Coat / Sheen / Transmission ほか）、Render Method、Backface Culling、Viewport Display |
| テクスチャ | Base Color Texture をはじめ Principled の各入力に繋いだ Image Texture（同一画像はセッション中 1 回だけ送信） |
| Material Node Tree | Blender 標準 Shader Node のノード追加 / 削除・種類・名前・位置・input 値・ノード固有プロパティ・リンク・Material Output 接続（Principled / Emission / Diffuse / Glass / Transparent / Mix Shader / ColorRamp / Noise / Voronoi / Wave / Mapping / Texture Coordinate / Normal Map / Bump など）。Image Texture は参照（名前 / パス / Color Space）のみ |
| Pose | Pose Mode の Pose Bone Transform（Location / Rotation / Scale / Rotation Mode、最大 15Hz） |
| Armature / Bone | 接続中の Armature 新規作成、Bone の追加 / 削除 / Rename、head / tail / roll、parent / connected（Edit Mode 中もリアルタイム） |
| Skinning | Vertex Groups の作成 / 削除 / Rename、各頂点の Weight（Weight Paint 中もリアルタイム、Automatic Weights の結果も）、Armature Modifier とその参照・主要設定。相手側でも Pose に合わせてメッシュが変形する |
| Import | 接続中の GLB Import（階層・複数 Mesh・UV・マテリアル・テクスチャ付き） |
| Presence | 選択、Active Tool（Freebird があれば `fb:draw.stroke` 等 / なければ Blender のツール）、Freebird XR 使用時の HMD / 左右コントローラー（20Hz）/ ポインターのレイ |

同じ対象を 2 人が同時に編集した場合の扱い（Edit Mode / Weight Paint 中の保留、後に抜けた側が勝つ など）や各機能の詳細は [`docs/`](docs/) の各ドキュメントを参照。

## 既知の制限

- Collection 間の移動は未対応
- Undo は共有されない（各自の Undo は自分の Blender だけに効く）
- 権限管理なし（ルームコードを知っている人は誰でも参加・編集できる）
- 音声は内蔵していない（Discord などを併用）
- Material Node Tree など新しい同期機能はまだ alpha 品質
- 同期対象外：
  - Shader Node：Node Group の中身 / OSL Script / Custom・外部 Addon ノード / Frame / RGB Curves 等のカーブ形状 / Image Texture の画像本体（Node Tree 経由の場合）
  - Geometry Nodes、Compositor、World Shader
  - リグ：Constraints / IK / Drivers / Bone Envelope の形状 / Custom Shape / Bone Collection の詳細 / Rigify
  - アニメーション：Keyframe / Action / NLA
  - 法線、Armature 以外のモディファイア
- 大人数での利用はまだ最適化していない
- フルデータ同期が必要な場合は Multiuser 0.8.x と併用する設計余地あり（[`docs/research.md`](docs/research.md)）

## インストールと設定（参加する全員の PC で同じ）

1. Edit > Preferences > Add-ons > Install from Disk で `freebird_collab_addon.zip` を入れて **Freebird Collaboration Layer** を有効化
   （または `freebird_collab` フォルダを `C:\Users\<name>\AppData\Roaming\Blender Foundation\Blender\5.2\scripts\addons\` にコピーして有効化）
2. （任意）アドオン設定で **Display Name / My Color** を好みで設定。**それ以外（Connection / Relay URL / Check Relay）は触らなくて OK** です
   - Relay URL には既定の公開 Relay（`wss://freebird-relay.chickenos.workers.dev`）が最初から入っています。**参加者同士で同じ Room Code を入れて Join するだけ**でインターネット越しに接続できます
3. （Freebird XR を使う場合のみ）`freebird_plugin/freebird_collab_menu.py` を `C:\Users\<name>\.freebird\plugins\` にコピー → Freebird Settings で **Reload All**（VR メニューの **CUSTOM** に Create / Join / Leave Room が出る）

### 上級者向け：接続まわりの設定（通常は不要）

- **つながらないとき**：アドオン設定の **Check Relay** を押して `OK xxx ms` が出るか確認。`NG` ならネットワーク（社内ネットワークやファイアウォール）を確認し、**Window > Toggle System Console** の `[collab]` 行を見る
- **自分の Relay を使う**：Relay URL 欄を自分の Relay の URL に書き換える（`wss://...` / LAN なら `python3 relay/collab_relay.py` を動かした PC の `ws://192.168.x.x:7788`）。**参加者全員が同じ Relay URL にする必要がある**。Relay の立て方は補足資料 [`docs/internet-relay.md`](docs/internet-relay.md) を参照。欄の右の ↺ ボタン（または欄を空にする）で既定の Relay に戻る
- **Direct (IP address)**：Connection を Direct にすると Relay を使わず、ホストがポート 7788 で待ち受ける。ゲストは Code 欄の代わりに `Host IP` 欄へ IP（LAN や Tailscale 等の VPN）を入力。LAN / デバッグ用
- **v0.11.0 以前からのアップデート**：Relay URL を自分で入力していた場合は、アップデート後もその値がそのまま使われる。既定の Relay にそろえたいときは ↺ ボタンを押す
- **古い版との混在**：v0.11.0 以前のアドオンには Relay URL の既定値が無い。相手が古い版のままなら、相手のアドオンも v0.11.1 に更新してもらう（同期の中身は v0.11.0 と同じなので混在しても動くが、古い版では Relay URL の手入力が必要）

## 使い方

Relay URL は設定済みなので、**やることは「ホストが Create Room → 全員が同じ Room Code で Join Room」だけ**です。

| 手順 | ホスト | ゲスト |
| --- | --- | --- |
| 1 | 正本にしたい .blend を開き COLLAB パネル（または VR メニュー）で **Create Room** | |
| 2 | パネルに出る **6 文字の Room Code** を Discord などで伝える | その Room Code を COLLAB パネルの Code 欄に入れて **Join Room**（VR メニューの Join Room は、この欄のコードで参加） |
| 3 | | ホストのシーンが自分の Blender に読み込まれる |
| 4 | お互いの編集がリアルタイムに反映される。相手の選択枠・「名前 / ツール / Selected: Cube」ラベルが見える。Freebird XR を起動していれば相手の頭（ワイヤーキューブ）・手（ピラミッド）・レイも見える | |
| 5 | **Save Master Scene** で正本を保存（ゲストが押すとホスト側で保存される） | |
| 6 | **Leave Room** | **Leave Room** |

音声は Discord などを使う（内蔵しない）。

## 構成

```
FreebirdCollabo/
├─ freebird_collab/          Blender アドオン本体（scripts/addons にフォルダごと置く）
│   ├─ __init__.py           UI・オペレーター・タイマー・公開API
│   ├─ session.py            同期ロジック（正本共有 / Transform / オブジェクトデータ / マテリアル / Pose / Presence）
│   ├─ object_data.py        Mesh/Curve/Grease Pencil/Text/Light/Camera/Empty/Armature・マテリアル・Node Tree の内容をシリアライズ・in-place 適用
│   ├─ presence.py           相手の頭・手・レイ・選択枠・ラベルの GPU 描画
│   ├─ hub.py                ROOM ハブ（relay サーバー兼 direct モードのホスト内蔵サーバー）
│   ├─ link.py               TCP クライアント（受信スレッド → メインスレッドへキュー）
│   ├─ protocol.py           メッセージ定義（4byte 長 + JSON）
│   └─ ws.py                 WebSocket 実装（stdlib のみ）— 無料 HTTP ホスティングに Relay を置くため
├─ freebird_collab_addon.zip  アドオンの配布用 zip（v0.11.1。Preferences > Add-ons > Install from Disk でも入れられる）
├─ freebird_plugin/
│   └─ freebird_collab_menu.py   Freebird VR メニューに COLLAB ボタンを足すプラグイン（Freebird XR 使用時のみ）
├─ relay/collab_relay.py     中継サーバー（依存なし・TCP と WebSocket を同一ポートで自動判別）
├─ relay/cloudflare/         Cloudflare Workers + Durable Objects 版 Relay（無料・固定URL・常時起動）
├─ relay/quick_tunnel.bat/.py  ホスト PC で relay + cloudflared quick tunnel を 1 発起動（アカウント不要）
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
├─ tests/test_relay_default.py Relay URL 既定値テスト（Room Code だけで接続・自前 URL・空欄フォールバック・Reset・Direct 非影響）
├─ tests/test_glb_real.py    実 GLB ファイルを使った Import 同期テスト（COLLAB_GLBS でパス指定）
├─ docs/research.md          調査メモ・アーキテクチャ・リスク
├─ docs/internet-relay.md    自分で Relay を立てたい人向けの補足資料（Cloudflare Workers / quick tunnel / Python relay。通常は読まなくてよい）
├─ docs/machida-merge-v0.4.md 外部からの修正提案の統合レビュー（採用/不採用/理由）・同期対象一覧
├─ docs/glb-import-v0.5.md   接続中の GLB Import 同期（原因・階層/マテリアル対応・未対応事項）
├─ docs/textures-v0.6.md     Base Color テクスチャ＋UV の転送（画像の再送なし・未対応事項）
├─ docs/textures-v0.6.1-sanze.md 実 GLB 2 種でのテクスチャ転送比較調査
├─ docs/material-sync-v0.7.md マテリアル同期（同期範囲・メッセージ・ループ防止・実機確認手順）
├─ docs/pose-sync-v0.8.md    Pose Mode ボーン Transform 同期（同期範囲・ループ防止・再接続・実機確認手順）
├─ docs/armature-sync-v0.9.md Armature / Bone 構造同期（同期範囲・Edit Mode 衝突時の扱い・Pose との順序・実機確認手順）
├─ docs/skinning-sync-v0.10.md Vertex Group / Skinning 同期（同期範囲・Weight Paint 中の扱い・Armature Modifier の参照解決・実機確認手順）
└─ docs/material-node-sync-v0.11.md Shader Node Tree 同期（payload・差分・Image Texture 参照・対象外ノード・後方互換・実機確認手順）
```

## テスト

テストの実行に VR ヘッドセットは不要です。

```
pip install bpy==5.0.1          # Blender を pip の bpy モジュールとして使う
python3 tests/test_sync.py direct   # LAN direct
python3 tests/test_sync.py relay    # tcp relay
python3 tests/test_sync.py ws       # websocket relay
python3 tests/test_sync.py wss      # websocket over TLS (local self-signed terminator)
python3 tests/test_data.py direct   # object data sync (same modes as above)
python3 tests/test_grease_pencil.py direct  # Grease Pencil create/draw/edit sync
python3 tests/test_materials.py direct  # material sync (same modes as above; "unit" = single-process part only)
python3 tests/test_material_nodes.py direct  # shader node tree sync
python3 tests/test_glb.py direct    # GLB import during a session (same modes as above)
python3 tests/test_pose.py direct   # Pose Mode bone transform sync
python3 tests/test_armature.py direct  # Armature / Bone structure sync
python3 tests/test_skinning.py direct  # Vertex Group / Skinning sync
python3 tests/test_relay_default.py    # Relay URL preset (room-code-only join, custom URL, Reset, Direct unchanged; "live" = also Check Relay)
```

`tests/test_sync.py` はホスト / ゲスト 2 プロセス（+ relay）を起動し、Create → Join → 正本共有 → Cube 移動の双方向同期 → 新規オブジェクト → 選択 / ツール presence → ホスト保存 までを自動検証する（PASS 済み）。

## ライセンス

FreebirdCollabo は **GNU General Public License v2.0 以降**（`GPL-2.0-or-later`）で公開しています。
ライセンス全文は [LICENSE](LICENSE) を参照してください。主なソースファイルの先頭にも `SPDX-License-Identifier: GPL-2.0-or-later` を記載しています。

Freebird XR は別製品で、このリポジトリには含まれていません。

## FreebirdCollabo を応援する 🐔

FreebirdCollabo は無料のオープンソースです。
開発を応援していただける場合は、こちらの [Amazon ほしい物リスト](https://www.amazon.jp/hz/wishlist/ls/2MNMSWIJ1FCB6?ref_=wl_share) からどうぞ。

もちろん任意です。不具合の報告や感想、まわりの人に紹介していただけることも、同じくらい励みになります。ありがとうございます！
