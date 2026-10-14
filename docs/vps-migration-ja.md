# VPS へのリレー移行ガイド（Python 版リレー + Caddy）

> **いますぐ移行する資料ではありません。** 「Cloudflare の無料枠が足りなくなったら、いつでも VPS に移れる」ための準備です。
> 移行するかどうかの判断は §3（ダッシュボードの数字で判断）を見てください。

対象：`relay/collab_relay.py`（Python 版リレー）と `relay/vps/`（VPS 用の設定一式）。
想定：ConoHa VPS などの低価格 Linux VPS、**Ubuntu 24.04**（22.04 でも同じ手順で動きます）。

---

## 0. 先に結論

- Python 版リレーは、Cloudflare 版と**同じ通信内容**を話します。batch（まとめ送り）、ping、`/stats` も対応済み。**配布済みの v0.12.1 アドオンも、新しいアドオンも、混ざったまま接続できます**（Docker で再現した環境で確認済み。§10）。
- VPS 版の構成は「**Caddy（wss:// と証明書の自動取得）→ Python リレー（systemd で常時起動）**」。必要なファイルは `relay/vps/` にそろっています。
- 移行の目安：**Cloudflare の Durable Objects のリクエスト数が、1 日 8 万を超える日が週に何度も出てきたら**（無料枠は 1 日 10 万）。§3 に表があります。
- 切り替えは「Blender の Relay URL 欄を書き換えるだけ」。**戻すのも ↺ ボタン 1 回**。Cloudflare 版は止めずに残しておけるので、いつでも戻れます。

---

## 1. 全体像

```
                   ┌──────────────── VPS（Ubuntu） ────────────────┐
Blender ──wss://──▶│ :443 Caddy ──▶ 127.0.0.1:7788 collab_relay.py │◀──wss:// ── Blender
                   │  ・証明書を自動で取得、更新   ・systemd で常時起動  │
                   │  ・アクセスログ（自動で古いものを削除）・上限と /stats │
                   │ ufw：開いているのは 22（SSH）/ 80 / 443 だけ        │
                   └───────────────────────────────────────────────┘
```

| ファイル（`relay/vps/`） | 役割 | VPS での置き場所 |
| --- | --- | --- |
| `freebird-relay.service` | systemd サービス。自動起動、落ちたら 3 秒後に自動再起動、専用ユーザー `freebird` で実行 | `/etc/systemd/system/` |
| `freebird-relay.env` | 上限などの設定値（§11） | `/etc/freebird-relay.env` |
| `Caddyfile` | wss:// 化（リバースプロキシ）と証明書の自動取得、アクセスログのローテーション | `/etc/caddy/Caddyfile` |
| `journald-freebird.conf` | リレーのログ（journal）のローテーション | `/etc/systemd/journald.conf.d/freebird.conf` |
| `ufw-setup.sh` | ファイアウォール（22/80/443 だけ開ける） | 実行するだけ |
| `Dockerfile` / `docker-compose.yml` | 同じ構成を Docker で再現（手元での確認用。VPS でも使える） | — |

---

## 2. Cloudflare 版と Python 版の機能差

| 機能 | Cloudflare 版（`relay/cloudflare/`） | Python 版（`relay/collab_relay.py`） |
| --- | --- | --- |
| 通信方式 | WebSocket（wss://） | WebSocket と生 TCP を同じポートで自動判別。VPS では Caddy が wss:// にする |
| ルームコード・hello / welcome / peer_join / peer_leave | あり | あり（同じ） |
| `to`（宛先指定）・`from`（送り主の刻印） | あり | あり（同じ） |
| **batch（まとめ送り）** | 1 通ずつにほどいて転送。`welcome.batch = 1` | **同じ**（`freebird_collab/hub.py` に実装済み）。古いアドオンには 1 通ずつ届く |
| **ping の応答** | ランタイムが自動応答（Durable Object を起こさない） | リレーがその場で `pong` を返す（VPS は料金に関係しないので同じ扱いで問題なし） |
| **`/stats`** | `{"sockets": N, "rooms": M}` | 同じ 2 項目 ＋ `frames_per_min`（受信メッセージ数/分。batch は 1 と数える＝Cloudflare の課金と同じ数え方）、`messages_per_min`、`kb_per_min`、`rejected`、`kicked`、`uptime_s`。部屋コードは出さない |
| 稼働状況のログ | Cloudflare のダッシュボード | 60 秒ごとに `[stats] sockets=.. rooms=.. frames/min=..` を 1 行（`journalctl` で見られる） |
| 1 メッセージの大きさ | 1 MiB まで（Cloudflare の制限） | 既定 4 MiB まで（設定で変更可）。アドオンは 700 KB で自動分割するので、どちらでも問題なし |
| 同時接続数・ルーム数の上限 | 実質なし（無料枠のリクエスト数が先に尽きる） | **既定 200 接続 / 100 ルーム**（設定で変更可）。超えたら新しい人にだけ「relay is full」を返す |
| 1 接続あたりの送信量の上限 | なし | **既定 6,000 メッセージ/分・512 MiB/分**（短い集中は通す方式）。超えた人だけ `relay limit: ...` を表示して切断。同じルームの他の人はそのまま |
| つないだまま何も送らない接続 | Cloudflare が管理 | hello まで 20 秒、その後は 300 秒何も来なければ切断（アドオンは 20 秒ごとに ping を送るので、普通は起きない） |
| 料金の数え方 | 受信 20 メッセージ = 1 リクエスト。無料は 10 万/日、超えると止まる | 月額固定。メッセージ数では増えない |
| 停止・再起動 | なし（Cloudflare が管理） | OS 更新の再起動などで数十秒止まることがある。落ちたら systemd が 3 秒後に自動で起動し直す |

補足（今回の調査で気づいたこと）：

- `relay/standalone/app.py` と `relay/hf_space/app.py`（1 ファイル版のリレー）は、batch 対応前の `hub.py` から作られた古いままです。batch を使わない（`welcome.batch` が無い）ので**新旧どちらのアドオンもつながります**が、まとめ送りの効果は出ません。VPS では使わず、`collab_relay.py` を使ってください（今回のファイルはすべてそちらを使います）。
- Python 版の上限・`/stats` は `collab_relay.py` だけに追加しました。アドオンに入っている `hub.py`（Direct モードでホストの Blender が使う）は変更していません。

---

## 3. Cloudflare 版と VPS 版の比較・移行すべきタイミング

### 3.1 比較

金額は **2026 年 10 月時点の目安**です。契約前に必ず公式ページで確認してください。

| | Cloudflare 無料（今） | Cloudflare 有料（Workers Paid） | VPS（ConoHa 1GB など）＋ドメイン |
| --- | --- | --- | --- |
| 月額の目安 | **0 円** | 約 750 円（$5〜。リクエスト 100 万/月込み、超過は 100 万あたり $0.15 程度） | VPS 約 700〜1,100 円 ＋ ドメイン約 100〜200 円/月（年 1,500〜2,500 円）＝ **約 800〜1,300 円** |
| 使いすぎた時 | **その日は 0:00 UTC（日本時間 9:00）までつながらなくなる** | 従量で少し増える | 増えない（CPU が足りなくなるまで） |
| 安定性 | 高い。サーバー管理なし | 高い | 1 台なので、OS 更新の再起動や障害で止まることがある。自動再起動はあり |
| 手間 | ほぼなし | ほぼなし（カード登録だけ） | 最初に 1〜2 時間。その後は月 1 回くらい OS 更新（§8） |
| 遅延（日本国内） | 良い | 良い | 良い（東京リージョンを選べば 10 ms 前後） |
| 1 メッセージの上限 | 1 MiB | 1 MiB | 自由（既定 4 MiB） |
| 他社への依存 | Cloudflare | Cloudflare | VPS 会社 ＋ ドメイン会社（乗り換えは簡単） |

処理能力の目安：このリポジトリの Python リレーを手元で計測すると、**CPU 1 コアで毎秒およそ 1 万メッセージ**を転送できました（`collab_relay.py`、ループバック。実際の VPS では半分くらいに見ておくと安全）。4 人の VR ルームで多くても毎秒 60 通ほど受信 × 3 人へ転送なので、**1 コア・1 GB の VPS でも、作業中のルームが 20〜30 個同時にあっても余裕**がある計算です。メモリは 1 接続あたり数 MB 以下。

### 3.2 いつ移行するか（ダッシュボードの数字で判断）

見る場所：<https://dash.cloudflare.com/> → **Workers & Pages** → `freebird-relay` → **Metrics**、または **Durable Objects** → `Hub` の **Requests**（1 日ごとの数）。
補助として `https://freebird-relay.chickenos.workers.dev/stats` の `sockets`（今つながっている人数）と `rooms`。

| ダッシュボードの数字（Durable Objects の Requests / 日） | 状況 | やること |
| --- | --- | --- |
| **5 万未満**の日がほとんど | 余裕あり | 何もしない。月 1 回くらい数字を見る |
| **5〜8 万**の日が週 2 回以上 | 黄色信号 | VPS とドメインの下調べ（§4.1）。新しいアドオンへの更新をみんなにお願いする（新しいアドオンは通信量が 1/2〜1/20 になる。`docs/relay-load-analysis.md`） |
| **8 万超え**の日が週 2 回以上、または **10 万に達した日がある**（その日はつながらなくなった人がいるはず） | 赤信号 | **移行する**（VPS ＋ この資料の §4〜§6）。または Cloudflare の有料プランにする（§3.3） |
| `/stats` の `sockets` がいつ見ても **20 以上** | 利用者が増えている | 上と同じ判断。VPS なら `FREEBIRD_MAX_CONNECTIONS` に余裕があるかも確認 |

数字の感覚（`docs/relay-load-analysis.md` §6 の実測から。新しいアドオンどうしの場合）：

| 1 日の使われ方（全ルーム合計） | 1 日のリクエスト数 |
| --- | --- |
| 2 人がデスクトップで 1 時間作業（ずっとドラッグしているくらい） | 約 5,500 |
| 2 人が VR で 1 時間作業 | 約 8,000 |
| 4 人が VR で 1 時間作業 | 約 10,000 |
| 2 人がつないだまま 1 時間放置 | 約 350 |

つまり **「VR 2 人の作業が 1 日合計 10 時間を超えはじめたら」赤信号** くらいの感覚です。v0.12.1 の人が混ざると 1.5〜3 倍になります。

VPS に移った後に「Cloudflare だったら何リクエスト相当か」を知りたい時は、VPS の `/stats` の `frames_per_min` を使います：
**`frames_per_min` ÷ 20 × 60 × （1 日に使っている時間）＝ Cloudflare の 1 日のリクエスト数に相当**。

### 3.3 VPS と Cloudflare 有料、どちらにするか

- **手間をかけたくない**なら Cloudflare 有料（Workers Paid）が一番簡単です（カード登録だけ。コードはそのまま）。月 $5 からで、今の使い方なら $5〜6 程度に収まる見込み。
- **月額を固定したい・Cloudflare の制限（1 MiB、リクエスト課金）から離れたい・自分で管理したい**なら VPS。
- どちらもすぐにはいりません。§3.2 の赤信号になってから決めれば間に合います（VPS の準備は §4 の手順で 1〜2 時間）。

---

## 4. VPS の準備（VPS を契約した後、ちきんがやる手順）

コマンドは**そのままコピーして貼り付け**できるように書いています。`relay.example.com` と書いてあるところは、自分のドメインに読み替えてください（§4.3 で一括置換するコマンドもあります）。

### 4.1 用意するもの

| もの | 例・メモ |
| --- | --- |
| VPS | ConoHa VPS、メモリ 1GB、**Ubuntu 24.04**、リージョンは東京。512MB でも動くが、OS 更新が重くなるので 1GB 推奨 |
| ドメイン | 例：`chickenos.com` を持っていれば、その下に `relay.chickenos.com` を作るだけ。持っていなければ年 1,500 円前後で買える（お名前.com、ConoHa ドメイン、Cloudflare Registrar など） |
| Windows の PowerShell | SSH 接続に使う（Windows 10/11 には最初から入っている） |

VPS を作るときの設定：
- OS：**Ubuntu 24.04**
- root パスワード：長くて推測されにくいもの（パスワード管理アプリに保存）
- SSH キー：作れるなら登録（ConoHa の画面で作成・ダウンロードできる）。難しければパスワードのままでも OK（§9 のチェックリストで後から強化）
- ConoHa の「セキュリティグループ」がある場合：**22, 80, 443 を許可**（または「IPv4v6-SSH」「IPv4v6-Web」などを付ける）

VPS ができたら、管理画面に出る **IPv4 アドレス**（例 `203.0.113.10`）をメモ。

### 4.2 ドメインの DNS 設定

ドメインの管理画面（買ったところ）の「DNS 設定」で、次の 1 行を追加します。

| 種類 | 名前（ホスト名） | 値 | TTL |
| --- | --- | --- | --- |
| **A** | `relay` | VPS の IPv4 アドレス（例 `203.0.113.10`） | 300（なければ既定のまま） |

- これで `relay.<あなたのドメイン>` が VPS を指すようになります。
- ドメインの DNS を Cloudflare で管理している場合は、その行の**プロキシ（オレンジの雲）をオフ＝「DNS のみ」（灰色の雲）**にしてください。
- 反映の確認（Windows の PowerShell）：
  ```
  nslookup relay.example.com
  ```
  `Address: 203.0.113.10`（VPS の IP）が出れば OK。数分〜1 時間かかることがあります。**これが出るまで §4.7 の証明書の取得はうまくいきません**。

### 4.3 VPS にログイン

PowerShell で（IP は自分のものに）：
```
ssh root@203.0.113.10
```
初回は `Are you sure you want to continue connecting (yes/no)?` → `yes`。パスワードを入力（画面には何も表示されないが、入力されている）。
プロンプトが `root@...:~#` になればログインできています。以下のコマンドはすべてこの画面に貼り付けます。

まず、自分のドメインを変数に入れておきます（**この 1 行だけ自分のドメインに書き換える**）：
```
export RELAY_DOMAIN=relay.example.com
```
※ SSH をつなぎ直したら、この行をもう一度実行してください。

### 4.4 OS を最新にする（5〜10 分）

```
sudo apt update && sudo apt -y upgrade
sudo apt -y install git python3 curl
sudo timedatectl set-timezone Asia/Tokyo
```
途中で紫の画面（再起動するサービスの確認など）が出たら、そのまま Enter。
最後に `python3 --version` で `Python 3.12.x`（22.04 なら 3.10.x）が出れば OK。

### 4.5 ファイアウォール（ufw）

リレーのファイルを先に取ってきて、その中のスクリプトを実行します。
```
sudo git clone --depth 1 https://github.com/niwanotorico/FreebirdCollabo.git /opt/freebird-relay
sudo sh /opt/freebird-relay/relay/vps/ufw-setup.sh
```
最後に次のような表が出れば OK（22 は「LIMIT」＝パスワードの総当たりを自動で弾く設定）：
```
22/tcp                     LIMIT IN    Anywhere
80/tcp                     ALLOW IN    Anywhere
443/tcp                    ALLOW IN    Anywhere
```
リレー本体のポート 7788 は**開けません**（VPS の中の Caddy からしか使わないため）。

### 4.6 リレーを常時起動にする（systemd）

```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin freebird
sudo cp /opt/freebird-relay/relay/vps/freebird-relay.env /etc/freebird-relay.env
sudo cp /opt/freebird-relay/relay/vps/freebird-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freebird-relay
```
確認：
```
systemctl status freebird-relay --no-pager
curl http://127.0.0.1:7788/stats
```
- `Active: active (running)` と出る
- `{"sockets": 0, "rooms": 0, ...}` が出る

これで、VPS を再起動しても自動で起動し、落ちても 3 秒後に自動で起動し直します。

### 4.7 wss:// にする（Caddy・証明書は自動）

Caddy をインストール（Caddy 公式の手順そのまま。5 行まとめて貼り付けて OK）：
```
sudo apt -y install debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt -y install caddy
```
（うまくいかない時は `sudo apt -y install caddy` だけでも OK。Ubuntu 付属の少し古い Caddy が入りますが、この Caddyfile はそれでも動くことを確認済み）

設定ファイルを置いて、ドメインを書き換えて、反映：
```
sudo cp /opt/freebird-relay/relay/vps/Caddyfile /etc/caddy/Caddyfile
sudo sed -i "s/relay.example.com/$RELAY_DOMAIN/" /etc/caddy/Caddyfile
grep "$RELAY_DOMAIN" /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
- `grep` の行で自分のドメインが表示される
- `Valid configuration` と出る

証明書の取得は自動で、普通は 1 分以内に終わります。様子を見るには：
```
sudo journalctl -u caddy --no-pager -n 30
```
`certificate obtained successfully` が出れば成功。

### 4.8 ログのローテーション

リレーのログ（journal）が増えすぎないようにします。Caddy のアクセスログは Caddyfile の設定で自動ローテーション済み（10 MB × 5 個、30 日）。
```
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp /opt/freebird-relay/relay/vps/journald-freebird.conf /etc/systemd/journald.conf.d/freebird.conf
sudo systemctl restart systemd-journald
journalctl --disk-usage
```

### 4.9 動作確認

**① ブラウザ**（どの PC でも）
- `https://relay.example.com/` → `freebird-collab relay ok`
- `https://relay.example.com/stats` → `{"sockets": 0, "rooms": 0, ...}`
- 鍵マークが付いている（証明書が有効）

**② PC から接続テスト**（Blender 不要。FreebirdCollabo のフォルダで、Python が入っている PC）
```
python relay\relay_check.py wss://relay.example.com
```
`OK  relay reachable, test room XXXXXX created  (xx ms)` が出れば OK。

**③ Blender**（§5 の「自分たちだけで試す」と同じ）

**④ VPS のログ**
```
journalctl -u freebird-relay -f
```
Blender で Join すると `[hub] 名前(guest) joined room XXXXXX` が流れる。60 秒ごとに `[stats] ...` が出る。Ctrl+C で見るのをやめる（リレーは止まらない）。

---

## 5. Cloudflare 版から切り替える方法

アドオンの既定の Relay URL は Cloudflare 版（`wss://freebird-relay.chickenos.workers.dev`）のままです。**切り替えは各自の Blender の設定で行います。** Cloudflare 版は止めずに残しておきます（いつでも戻れるように）。

> **大事：同じルームに入る人は、全員が同じ Relay URL にしてください。** Cloudflare 版で作ったルームのコードは、VPS 版では「room not found」になります（逆も同じ）。

### 5.1 まず自分たちだけで試す（1〜2 日）

1. Blender → Edit → Preferences → Add-ons → Freebird Collaboration Layer
2. Relay URL を `wss://relay.example.com` に書き換える → **Check Relay** → `OK xx ms`
3. 2 台（できれば **v0.12.1 のアドオン**と**新しいアドオン**を 1 台ずつ）で：
   - Create Room → Join → シーンが読み込まれる
   - Cube を動かす → お互いに反映される
   - 30 秒放置しても相手の名前・選択枠が消えない
   - VR の人がいれば、頭・手・レイが表示される
   - Leave → もう一度 Join → 元どおり
4. ブラウザで `https://relay.example.com/stats` → 作業中は `sockets` が人数分、`rooms` が 1

### 5.2 みんなに切り替えてもらう

友人に送る文面の例：
```
コラボ用のリレーを新しくしました。
Blender → Edit → Preferences → Add-ons → Freebird Collaboration Layer の
Relay URL を  wss://relay.example.com  に書き換えて、Check Relay で OK が出るか確認してください。
※ 一緒に作業する人は全員同じ URL にしてください。
※ 調子が悪い時は Relay URL 横の ↺ ボタンで元に戻せます。
```

全員の切り替えが終わると、Cloudflare のダッシュボードの Requests がほぼ 0 になります。

（将来、アドオンの次のバージョンで既定の Relay URL を VPS に変えれば、この手作業は不要になります。今回はバージョン番号を変えない方針なので、やっていません）

---

## 6. 問題が出た時に戻す方法

### 6.1 すぐに Cloudflare 版に戻す（各自・10 秒）

Preferences の Relay URL 横の **↺ ボタン**（または欄を空にする）→ 既定の Cloudflare 版に戻ります。同じルームの人は全員そろえてください。
Cloudflare 版は止めていないので、そのまま使えます。

### 6.2 VPS のリレーだけ再起動

```
sudo systemctl restart freebird-relay
```

### 6.3 VPS のリレーを更新した後に調子が悪い時（ひとつ前に戻す）

```
cd /opt/freebird-relay
sudo git log --oneline -5              ← 戻したいコミットの番号（左の 7 文字）を確認
sudo git checkout <コミット番号>
sudo systemctl restart freebird-relay
```
元の最新に戻すときは `sudo git checkout main && sudo git pull` → `sudo systemctl restart freebird-relay`。

### 6.4 VPS をやめる

みんなの Relay URL を ↺ で戻してもらってから、VPS を解約。ドメインの A レコードも消しておく。

---

## 7. 困った時

| 症状 | 原因と対処 |
| --- | --- |
| ブラウザで `https://relay.../` が開かない | `nslookup` で VPS の IP が出るか（§4.2）。ufw で 80/443 が開いているか（`sudo ufw status`）。ConoHa のセキュリティグループで 80/443 が許可されているか。`sudo journalctl -u caddy -n 50` |
| Caddy のログに証明書のエラー（`challenge failed` など） | DNS がまだ反映されていない／80 番が閉じている。直したら `sudo systemctl restart caddy` |
| ブラウザで `502 Bad Gateway` | リレーが止まっている → `systemctl status freebird-relay`、`journalctl -u freebird-relay -n 50` |
| Blender に `relay is full, try again later` | 同時接続数かルーム数の上限。`/stats` を見て、多すぎなければ `/etc/freebird-relay.env` の上限を上げて `sudo systemctl restart freebird-relay` |
| Blender に `relay limit: too many messages per minute` など | その人だけ送信量の上限を超えた（普通の使い方では起きない数字にしてある）。ログの `[limit]` 行を確認し、必要なら上限を上げる |
| Blender に `room XXXXXX not found` | ホストと違う Relay URL を見ている（Cloudflare 版と VPS 版が混ざっている）／コード違い |
| Blender に `websocket handshake refused: HTTP/1.1 503` | 同時接続数の上限（上と同じ） |
| VPS を再起動した | 何もしなくてよい（リレーも Caddy も自動で起動する）。作業中だった人は Join し直し |

---

## 8. ふだんの手入れ（月 1 回・5 分）

```
sudo apt update && sudo apt -y upgrade
cd /opt/freebird-relay && sudo git pull
sudo systemctl restart freebird-relay
```
- `*** System restart required ***` と出たら、**誰も作業していない時間に** `sudo reboot`（1 分ほどで戻る）。
- `sudo systemctl restart freebird-relay` や `sudo reboot` の瞬間、作業中の人は切断されます（Join し直せば続きから）。
- Caddy の証明書の更新は自動（何もしなくてよい）。

---

## 9. VPS で実際に確認が必要なことチェックリスト

手元（Docker・systemd 入りコンテナ）では確認済みですが、**本物の VPS・本物のドメインでしか確かめられない**項目です。

**構築直後**
- [ ] `nslookup relay.<ドメイン>` で VPS の IP が返る
- [ ] Caddy が **Let's Encrypt の本物の証明書**を取れた（ブラウザで鍵マーク、`journalctl -u caddy` に `certificate obtained successfully`）
- [ ] `https://relay.<ドメイン>/` が `freebird-collab relay ok`、`/stats` が JSON
- [ ] `http://relay.<ドメイン>/` が https に自動で転送される
- [ ] 外から 7788 番に直接つながらない（PowerShell で `Test-NetConnection relay.<ドメイン> -Port 7788` が `TcpTestSucceeded : False`）
- [ ] `sudo ufw status` が 22/80/443 だけ
- [ ] `systemctl status freebird-relay` の `User` が `freebird`（`ps -o user= -C python3` でも確認できる）
- [ ] `sudo reboot` の後、何もしなくてもリレーと Caddy が起動している
- [ ] `sudo systemctl kill -s KILL freebird-relay` の後、3 秒ほどで自動で起動し直す（`systemctl status` の起動時刻が新しくなる）

**Blender で（実機）**
- [ ] Check Relay → `OK xx ms`（Windows の Blender 同梱 Python で証明書エラーが出ない）
- [ ] v0.12.1 どうしで Create / Join / Cube 移動 / Leave
- [ ] 新しいアドオンどうしで同じこと
- [ ] v0.12.1 と新しいアドオンを混ぜて同じこと（選択枠・ツール名が 30 秒放置しても消えない）
- [ ] VR（Freebird）で頭・手・レイ・ポーズ・マテリアルが同期する
- [ ] 大きめのシーン（数十 MB、テクスチャ入り）で Join → 読み込まれる（`kicked` が増えない）
- [ ] 1 時間以上つなぎっぱなしで切れない
- [ ] 自宅回線・モバイル回線（テザリング）など、別のネットワークからもつながる

**数日後**
- [ ] `journalctl -u freebird-relay | grep '\[limit\]'` に、普通の使い方での切断が出ていない（出ていたら上限が低すぎる）
- [ ] `journalctl --disk-usage` が増え続けていない
- [ ] `sudo ls -la /var/log/caddy/` のログが 10 MB を超えたら分割されている

**任意（安全のため、慣れてきたら）**
- [ ] SSH をパスワードではなく鍵でログインするようにする
- [ ] Ubuntu の自動セキュリティ更新（`unattended-upgrades`）が有効（Ubuntu 24.04 は既定で有効。`systemctl status unattended-upgrades`）

---

## 10. 開発者向け：Docker で手元確認（Windows / Mac / Linux）

VPS と同じ構成（Caddy → Python リレー）を、手元の Docker で再現できます。Caddy が `localhost` 用の証明書を自分で発行します。

```
sh tools/vps_docker_test.sh
```
やっていること：`relay/vps/docker-compose.yml` を起動 → Caddy の証明書を取り出して信頼 → `wss://localhost:8443` に対して
`relay_check.py`、`test_sync.py wss`（新アドオン 2 人）、`test_relay_traffic.py unit`（batch など通信内容）、`test_relay_traffic.py live`（**新・新・v0.12.1 の 3 人混在**）→ 片付け。`bpy` の入った Python が必要です。

手で起動する場合：
```
cd relay/vps
RELAY_DOMAIN=localhost HTTP_PORT=8080 HTTPS_PORT=8443 docker compose up -d --build
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./root.crt
SSL_CERT_FILE=./root.crt python3 ../relay_check.py wss://localhost:8443
docker compose down -v
```

VPS で systemd ではなく Docker を使いたい場合は、`RELAY_DOMAIN=relay.<ドメイン> docker compose up -d --build`（80/443 を使う）。この場合も §4.2（DNS）と §4.5（ufw）は必要です。

### テスト結果（このブランチ・bpy 5.1.2）

- `tests/test_relay_vps.py`（新規）：上限・`/stats`・既存のルーティング試験一式（ws と tcp）— PASS
- Docker（Caddy + リレー、`wss://localhost:8443`）：`test_sync.py wss`、`test_relay_traffic.py unit`、`test_relay_traffic.py live`（新・新・v0.12.1 混在）— PASS
- Docker 越しに上限を下げて：2 人目の接続が `HTTP/1.1 503`、送りすぎた人に `relay limit: too many messages per minute` が届くことを確認
- systemd が動くコンテナ（Ubuntu 24.04）で、手順 §4.5〜§4.8 をそのまま実行：`freebird` ユーザーで起動、`kill -9` で自動再起動、`systemctl stop` では再起動しない、Ubuntu 付属 Caddy 2.6.2 で `wss://localhost` に `relay_check` OK、ufw は 22(LIMIT)/80/443 のみ

---

## 11. 設定値（`/etc/freebird-relay.env`）

変更したら `sudo systemctl restart freebird-relay`（作業中の人は切断されるので、空いている時間に）。`0` は「制限なし／オフ」。
コマンドラインでも同じ名前で指定できます（`python3 relay/collab_relay.py --help`）。

| 設定 | 既定 | 意味 | 超えた時 |
| --- | ---: | --- | --- |
| `FREEBIRD_MAX_CONNECTIONS` | 200 | サーバー全体の同時接続数 | 新しく来た人に `relay is full`（WebSocket は HTTP 503） |
| `FREEBIRD_MAX_ROOMS` | 100 | 同時に開いているルーム数 | 新しくルームを作ろうとした人に `relay is full (too many rooms)`。既存ルームへの Join はできる |
| `FREEBIRD_MAX_MSGS_PER_MIN` | 6000 | 1 接続が 1 分に送れるメッセージ数（batch は 1 と数える）。v0.12.1 で VR 操作中が多くても約 3,800 | その人だけ `relay limit: too many messages per minute` → 切断 |
| `FREEBIRD_MAX_MB_PER_MIN` | 512 | 1 接続が 1 分に送れる量（MiB）。1 分ぶんまでは一気に送れるので、大きいシーンの送信も通る | その人だけ `relay limit: too much data per minute` → 切断 |
| `FREEBIRD_MAX_MESSAGE_MB` | 4 | 1 メッセージの最大（MiB）。アドオンは 0.7 MiB で自動分割 | その人だけ `relay limit: message too large` → 切断 |
| `FREEBIRD_HELLO_TIMEOUT` | 20 | つないでからルームに入るまでの秒数 | 切断（何も話さない接続の掃除） |
| `FREEBIRD_IDLE_TIMEOUT` | 300 | ルームに入った後、何も届かない秒数（アドオンは 20 秒ごとに ping） | 切断 |
| `FREEBIRD_STATS_INTERVAL` | 60 | `[stats]` ログを出す間隔（秒） | — |
| `FREEBIRD_STATS_ENDPOINT` | 1 | `1` で `/stats` を公開（合計の数字だけ。部屋コードは出さない）、`0` で非公開 | — |

`/stats` の項目：

| 項目 | 意味 |
| --- | --- |
| `sockets` / `rooms` | 今の接続数とルーム数（Cloudflare 版と同じ） |
| `frames_per_min` | 直近 1 分に受け取ったメッセージ数（batch は 1）。**÷ 20 で Cloudflare のリクエスト数/分に相当** |
| `messages_per_min` | 同じく、batch をほどいた後の数 |
| `kb_per_min` | 直近 1 分に受け取った量（KB） |
| `rejected` | 起動してから、満員で断った接続・ルームの数 |
| `kicked` | 起動してから、送信量の上限で切断した数 |
| `uptime_s` | 起動してからの秒数 |
