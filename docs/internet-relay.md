# インターネット越し ROOM コード参加 — 公開手順と実機テスト（v0.3）

> **この資料は、自分で Relay を立てたい人向けの補足です。通常は読まなくて大丈夫です。**
> アドオンの Relay URL には既定の公開 Relay があらかじめ入っているので、ふつうに使うだけなら参加者同士で同じ Room Code を入力して Join するだけで接続できます（README の「セットアップ」「使い方」参照）。
> 自前の Relay を使う場合は、下の手順で作った URL を参加者全員の Relay URL 欄に入れてください（↺ ボタンで既定の Relay に戻せます）。

## 仕組み

```
HOST Blender ──wss──▶ Relay（HTTPS/WebSocket・固定URL）◀──wss── 友人 Blender
```

- Relay は「部屋コード → 接続の振り分け」だけ。シーンデータは通過するだけで保存しない
- アドオンは `tcp://` `ws://` `wss://` を受ける。LAN の **Direct モードと Python Relay は従来通り**（無変更）
- Relay の実装は 2 つ、**同じプロトコル**：
  - `relay/collab_relay.py` … Python（LAN / VPS / quick tunnel 用）
  - `relay/cloudflare/` … **Cloudflare Workers + Durable Objects**（無料・カード不要・固定URL・スリープなし）
- ※ Hugging Face Spaces は 2026-07 から Docker/Gradio が有料化されたため不採用

## 選択肢

| 案 | 費用 | アカウント | 固定URL | 用途 |
| --- | --- | --- | --- | --- |
| **③ quick tunnel**（3090 PC で relay + cloudflared） | 無料 | **不要** | ✕ 起動ごとに変わる | 今日すぐ友人と接続確認 |
| **① Cloudflare Workers + DO** | 無料 | Cloudflare（メールのみ） | ○ `xxx.workers.dev` | 本命。常時起動 |
| ② Render Free | 無料 | Render | ○ | 予備（15 分でスリープ、復帰 1 分） |
| Direct / Python relay | 無料 | 不要 | LAN 内 | 従来通り |

---

## ③ 今日すぐ：quick tunnel（アカウント不要）

**3090 PC（ホスト）で 1 回だけ準備**
1. https://github.com/cloudflare/cloudflared/releases から `cloudflared-windows-amd64.exe` をダウンロードし、`FreebirdCollabo\relay\` に **`cloudflared.exe` という名前で**置く
2. Python 3 が入っていなければ https://www.python.org/downloads/ から入れる（無ければ .bat が Blender 同梱 python を探す）

**毎回**
1. `relay\quick_tunnel.bat` をダブルクリック → 黒い窓に
   ```
   Relay URL: wss://xxxx-xxxx-xxxx.trycloudflare.com
   ```
   と出る（クリップボードにもコピー済み）。**窓は閉じない**
2. その URL を友人に送る（アドオン zip もまだなら一緒に）
3. **両方の PC**：Blender > Edit > Preferences > Add-ons > Freebird Collaboration Layer
   - Connection: **Relay server (room code)**
   - Relay URL: 上の `wss://...`
   - **Check Relay** → `OK xxx ms`
4. ホスト：N パネル COLLAB > **Create Room** → 6 文字コードを友人に送る
5. 友人：Code 欄にコード → **Join Room** → ホストのシーンが読み込まれる → Cube を動かして相互に反映されれば成功

URL は quick_tunnel を起動し直すたびに変わるので、次回は 1〜3 をやり直す（①ができたら不要になる）。

---

## ① 本命：Cloudflare Workers + Durable Objects（固定 URL・常時起動）

無料枠：10 万リクエスト/日（WebSocket 受信 20 メッセージ = 1 リクエスト換算）。2 人で 1 日 5 時間程度の共同作業が目安。

**ちきんさんの操作（10 分）**
1. https://dash.cloudflare.com/sign-up でアカウント作成（メールだけ。カード不要）
2. Node.js が無ければ https://nodejs.org/ から LTS を入れる
3. コマンドプロンプトで
   ```
   cd C:\Users\studio\Documents\ChickenOS_3090\FreebirdCollabo\relay\cloudflare
   npm install
   npx wrangler login          ← ブラウザが開くので Allow
   npx wrangler deploy
   ```
4. 最後に `https://freebird-relay.<アカウント名>.workers.dev` と表示される。ブラウザで開いて `freebird-collab relay ok` が出れば完了
5. 両方の PC のアドオン設定 → Relay URL: **`wss://freebird-relay.<アカウント名>.workers.dev`** → Check Relay

以後、Relay URL は変わらない。友人には「アドオン zip」と「この URL」を 1 回渡すだけ。
状況確認：`https://.../stats` を開くと今の部屋と接続数が JSON で見える。

初回 `wrangler deploy` で「Durable Objects の SQLite クラスを有効化しますか」的な確認が出たら Yes。`workers.dev` サブドメインの作成を聞かれたら好きな名前を入れる。

---

## 実機テスト手順（超短縮版・どの Relay でも同じ）

| | HOST | JOIN（友人） |
| --- | --- | --- |
| 事前 | Preferences に Relay URL、Check Relay OK | 同じ |
| 1 | COLLAB > **Create Room** | |
| 2 | 出てきた 6 文字コードを送る | Code 欄に入力 → **Join Room** |
| 3 | | `JOINED room XXXXXX (scene from host)` |
| 4 | Cube を動かす → 相手に反映 | Cube を動かす → 相手に反映 |
| 5 | **Save Master Scene** | **Leave Room** |

うまくいかない時：Window > Toggle System Console の `[collab]` 行
- `join failed: room XXX not found` → コード間違い / ホストが Leave 済み / 別の Relay URL を見ている
- `relay URL is empty` → Preferences 未設定
- `[SSL: CERTIFICATE_VERIFY_FAILED]` → Blender 同梱 Python の証明書問題。報告してください（certifi 経由に切替済みなので出ない想定）
- `websocket handshake refused: HTTP/1.1 530` → quick tunnel 側が落ちている（黒い窓を確認）

## テスト結果（クラウド上 bpy 5.0.1）

- `tests/test_sync.py direct / relay / ws / wss` 各 PASS（v0.2 からの回帰なし）
- **Cloudflare DO relay**（`wrangler dev` ローカル実行）経由で `test_sync.py ws` を 3 回連続 PASS、5 MB シーンの分割送信（chunk）と peer_leave も確認
- 実インターネット経由の検証は、③ と ① のデプロイ後に実機で行う

## v0.3 の変更

- Cloudflare Workers/DO の 1 MiB メッセージ上限に合わせ、アドオン側で 700 KB 超のメッセージを自動分割・再結合（全モード共通、Relay 側は無変更で通る）
- `relay/quick_tunnel.py` / `.bat`：relay + cloudflared を 1 発起動して wss URL を表示
- `relay/cloudflare/`：Workers + Durable Objects 版 Relay（`src/index.js`, `wrangler.toml`）
