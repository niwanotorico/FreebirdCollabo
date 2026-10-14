# 公開リレーの負荷調査と削減（Cloudflare 無料枠対策）

対象：`relay/cloudflare/`（Workers + Durable Objects）と、アドオン側の通信処理（`freebird_collab/session.py` `hub.py` `protocol.py` `ws.py` `link.py`）。
きっかけ：Cloudflare から無料枠の警告が届いた。

> 先に結論
> - **一番の原因は Presence（頭・両手・選択・ツール）。変化がなくても、一人きりでも、1 人あたり毎秒約 19 回ずっと送っていた。** 2 人が開きっぱなしにするだけで、無料枠（10 万リクエスト/日）を約 14 時間で使い切る計算になる。
> - Hibernation API は**もう使っている**。ただ、Presence が 50 ms おきに届くので、実際には Durable Object が休む時間がなかった。
> - 今回の変更で、**2〜4 人の放置中は 95% 減、デスクトップで操作中は 55〜70% 減、VR で操作中は 34〜47% 減**（どれも実測。表は §6）。
> - 古いアドオン（v0.12.x）とも、古いリレーとも、そのまま混ぜて使える（§7）。
> - **ただし効果の大部分はアドオン側の変更によるもの。** リレーをデプロイしただけで減るのは ping の分（1% 未満）だけ。みんなが新しいアドオンに更新して、はじめて大きく減る（§8）。

---

## 1. 今の作り（調査結果）

```
Blender ──wss──▶ Worker (fetch) ──▶ Durable Object "hub"（1 個だけ。全ルームがここに入る）
                                     ├ acceptWebSocket()  … Hibernation API 対応済み
                                     ├ webSocketMessage() … 受け取ったら、同じルームの他の人（または msg.to）にそのまま転送
                                     └ serializeAttachment() に uid / name / role / color / room を保存
```

- **Hibernation API：使っている**（`this.ctx.acceptWebSocket(server)` と `webSocketMessage / webSocketClose / webSocketError`）。ただし `setWebSocketAutoResponse` は使っていなかったので、ping が来るたびに Durable Object が起きていた。
- Durable Object は `idFromName("hub")` で作った 1 個だけ。どのルームのメッセージも同じ 1 個が処理する。
- 1 メッセージを転送するたびに `getWebSockets()` で全員分の接続を見て、attachment を全部読み直している（ルーム数が増えるほど CPU を食う）。
- ストレージ（SQLite）には何も書いていない。今回の課金とは関係ない。

### アドオンが送っているもの（v0.12.1）

`CollabSession.tick()` は 60 Hz のタイマーで動いている（`TICK_INTERVAL = 1/60`）。

| 種類 | 送るタイミング | 1 人あたりの頻度 |
| --- | --- | --- |
| `presence` | **20 Hz で毎回**。変化がなくても、他に誰もいなくても送る | **約 1,160 回/分（実測）** |
| `xform` | 30 Hz でチェックして、動いたオブジェクトがある時だけ | ドラッグ中だけ約 1,700 回/分 |
| `pose` | 15 Hz でチェックして、ボーンが変わった時だけ | ポーズ編集中だけ最大 900 回/分 |
| `obj_data` / `mat` / `obj_mats` | 5 Hz でチェックして、変わった時だけ | 編集中だけ（メッシュは帯域上限あり） |
| `ping` | 20 秒ごと | 3 回/分 |
| `scene` / `img` / `chunk` | 参加時・テクスチャ追加時の 1 回だけ | ほぼ 0 |

受け取る側（v0.12.x の `presence.py`）は、**最後の presence から 3 秒（`STALE_SECONDS`）経つと、その人の頭・手・ラベルを描かなくなる**。presence を間引く時は、これを壊さないことが条件になる。

---

## 2. Cloudflare で何がカウントされるか

公式ドキュメント（Durable Objects の Pricing と WebSockets のページ）で確認した。
※作業環境のネットワーク制限で developers.cloudflare.com を直接開けなかったため、公式ページの検索結果の抜粋で確認している。数字が変わっていないか、デプロイ前にもう一度見てほしい：
<https://developers.cloudflare.com/durable-objects/platform/pricing/> ・ <https://developers.cloudflare.com/durable-objects/best-practices/websockets/>

| 項目 | 無料プラン | 今回の作りでの意味 |
| --- | --- | --- |
| リクエスト | **10 万/日**（00:00 UTC にリセット） | **WebSocket の受信 20 メッセージ = 1 リクエスト**（公式）。送信（DO → クライアント）は数えない、接続（Upgrade）1 回 = 1 リクエスト（この 2 点は公式の抜粋に直接は出てこず、第三者の解説による） |
| Duration | 13,000 GB-s/日（128 MB 単位で課金） | 「アイドルで Hibernation できる状態の DO は Duration 課金されない」。`setWebSocketAutoResponse` で返した応答は wall-clock 時間に入らない |
| 上限を超えたら | 無料プランは課金されず、その種類の処理がエラーになる | **リレーにつながらなくなる** |

**一番効いているのはリクエスト数**。

- 変更前、2 人が放置しているだけ：受信 2,316 msg/分（実測）→ 116 リクエスト/分 → **約 14 時間で 10 万に達する**。4 人なら約 7 時間。
- 1 人でルームを開いて待っているだけでも 1,160 msg/分 → 58 リクエスト/分（約 29 時間で上限）。
- Duration：Hibernation API を使っているので、公式の説明どおりなら処理していない時間は課金されない。ただ 50 ms おきにメッセージが来ると、Durable Object が実際に休める時間はほとんどない。どれくらい課金されているかは、ダッシュボードの数字で確認するのが確実（§9）。

---

## 3. 1 ルームあたりの見積り（変更前）

アドオンの送信スケジュールをそのまま再現したモデル（`tools/relay_load_sim.py`）での数字。実測は §6 にある。
※モデルはタイマーのずれを少なめに見ているので、presence が実測（1 人 1,160 回/分）より少し少なく出る（約 1,000 回/分）。

| 状況 | 受信 msg/分 | リクエスト/分 | 10 万リクエストに達するまで |
| --- | ---: | ---: | ---: |
| 1 人・待機中 | 1,004 | 50 | 33 時間 |
| 2 人・放置 | 2,016 | 101 | 16.5 時間 |
| 2 人・1 人がドラッグ中 | 3,419 | 171 | 9.8 時間 |
| 3 人・放置 | 3,028 | 151 | 11 時間 |
| 4 人・放置 | 4,038 | 202 | 8.3 時間 |
| 4 人・VR で 1 人が動きながらドラッグ | 5,420 | 271 | 6.2 時間 |

全ルームの合計が 1 日 10 万なので、2 人ルームが 2 つあれば、どちらも半分の時間で止まる。

---

## 4. 削減案（効果が大きい順）

| # | 案 | 効果 | 後方互換 | 今回 |
| --- | --- | --- | --- | --- |
| A | **Presence は変化した時だけ送る＋1 秒ごとの生存通知（heartbeat）**。VR のトラッキングの細かい揺れ（位置 2 mm・回転 約 0.2° 未満）は「変化なし」扱い | 放置中は presence が 1/19 になる。VR でも 2〜4 割減 | 古い受け手は 3 秒で表示が消えるので、heartbeat は 1 秒（3 秒に対して 3 倍の余裕）。古い送り手は今までどおり 20 Hz で送るだけ | ✅ |
| B | **一人しかいない時は Presence を送らない**。誰かが入ってきたら、すぐに 1 回送る | 待機中の 1 人分が 0 になる | 相手がいないので影響なし。参加してきた人には `peer_join` の直後に送る | ✅（A と同じ仕組み） |
| C | **1 tick 内の小さいメッセージを 1 通にまとめる**（`{"t":"batch","m":[...]}`）。リレーが中身を 1 通ずつ転送する | VR 操作中で約 2 割減（実測：2 人で 3,220 → 2,650。xform と presence が同じ tick に重なる分）。デスクトップの放置中は 0 | リレーが `welcome.batch = 1` と言った時だけ使う → 古いリレーには送らない。受け手には 1 通ずつ届くので古いアドオンもそのまま | ✅ |
| D | **ping をリレーの自動応答にする**（`setWebSocketAutoResponse`） | 1 人 3 回/分。小さいが、1 人で待っている時に Durable Object が完全に休めるようになる | アドオンが送る `{"t":"ping"}` は全バージョンでまったく同じ文字列なので、そのまま返る | ✅ |
| E | xform を 30 Hz → 20 Hz にする | ドラッグ中の xform が −33% | 互換は問題ないが、相手から見た動きがカクつく | ❌ ちきん判断（体感の問題） |
| F | 誰も見ていない Presence を間引く | 不明 | 受け手が「見ているか」を送る仕組みがない。古いアドオンはそれを送れないので、混ざると表示が消える | ❌ 見送り |
| G | ルームごとに Durable Object を分ける（`idFromName(room)`） | リクエスト数は**減らない**。CPU と休める時間は良くなる | ホストがコードなしで作る時の部屋コードを Worker 側で決める必要があり、`/stats` も作り直しになる | ❌ 今は不要（大きな作り直し） |
| H | VR の presence を 10 Hz にして受け手側で補間する | VR で −50% | 古い受け手は補間しないので動きがカクつく | ❌ 見送り |

A〜D は「互換を壊さない・変更が小さい・効果が大きい」ので実装した。E は互換ではなく見た目の問題なので、ちきんの判断に任せたい。F〜H は互換を壊すか、作り直しが大きいので見送った。

---

## 5. 実装したこと

| ファイル | 変更 |
| --- | --- |
| `freebird_collab/traffic.py`（新規） | `PresenceThrottle`（変化時だけ送る・heartbeat 1 秒・一人なら送らない・最大 20 Hz）、`presence_changed`（頭と手にだけ不感帯）、`batch_groups`（順番を保ったまままとめる。まとめる対象は小さい種類だけ、1 通は最大 256 KB） |
| `freebird_collab/session.py` | presence を `PresenceThrottle` 経由で送る。`peer_join` が来たら次の tick ですぐ送る。`tick()` 全体を `begin_batch / end_batch` で囲む。`welcome.batch` を見て、まとめて送ってよいか決める |
| `freebird_collab/link.py` | `begin_batch / end_batch`、`can_batch`、`frames_tx`（実際にソケットへ書いたメッセージ数。計測とテスト用） |
| `freebird_collab/hub.py` | `batch` を 1 通ずつに分けて転送する。`welcome` に `batch: 1` を追加。（Direct モードのホストと Python リレーで使われる） |
| `relay/cloudflare/src/index.js` | ping の自動応答、`batch` を分けて転送（同じルームの相手一覧は batch 1 通につき 1 回だけ調べる）、`welcome.batch = 1` |
| `relay/_netlib.py` | `traffic` も読み込む（`link.py` が使うようになったため） |
| `tools/relay_load_sim.py`（新規） | モデルでの見積り（Blender 不要） |
| `tools/relay_load_measure.py`（新規） | 実測：本物のアドオン（v0.12.1 と今回の版）を Blender プロセスで 2〜4 人分動かし、リレーが受け取ったメッセージを数える |
| `tests/test_relay_traffic.py`（新規） | 回帰テスト（§7） |

**触っていないもの**：`freebird_collab_addon.zip`、`release/`、`ADDON_VERSION`、`wrangler.toml`、DNS や本番の設定。
`relay/standalone/app.py` と `relay/hf_space/app.py`（`build_standalone.py` で作る 1 ファイル版）は、もともと最新の `hub.py` と同じ内容ではなかったので、今回は作り直していない。どちらも `welcome.batch` を返さないので、新しいアドオンは「古いリレー」とみなしてまとめ送りをしない。互換は保たれていて、presence の削減（A・B）はこちらでも効く。

### 判断したこと

- **heartbeat を 1 秒にした理由**：古い受け手の `STALE_SECONDS = 3.0` より十分短くする必要がある。2 秒だと、ネットワークの揺れで 1 回遅れるだけで表示がちらつく可能性がある。実測では、v0.12.1 の受け手から見た「最後の presence からの経過時間」の最大は 1.05 秒だった。
- **不感帯（2 mm / 約 0.2°）**：見た目ではほぼ分からない大きさで、VR で静止している時の手の震えを吸収できる。送るタイミングが来たら、まとめずに最新の値をそのまま送る（受け手の位置がずれたまま残ることはない）。
- **batch を「リレーが対応していると言った時だけ」にした理由**：古いリレー（今の本番を含む）は `batch` を知らないので、そのまま転送してしまう → 古いアドオンは中身を読めない。`welcome.batch` を見て判断すれば、リレーとアドオンをどちらから更新しても安全。
- **batch にまとめる種類を絞った理由**：`scene` / `img` / `obj_data` / `mat` は大きくなることがあり、`chunk` 分割（700 KB）とぶつかるので、まとめない。まとめるのは presence / xform / pose / ver / obj_mats / obj_del / mat_ren / mat_del / img_need だけ。
- **ping を batch に入れない理由**：単独で送れば自動応答で返り、Durable Object を起こさずに済む。

---

## 6. 変更前と変更後の比較

### 6.1 実測（`tools/relay_load_measure.py --seconds 30`）

本物のアドオンのコードを Blender（`bpy` 5.0.1）で人数分起動して、リレーが受け取ったメッセージを数えた。変更前 = git の v0.12.1（`da4b3fd`）、変更後 = このブランチ。リレーは同じ `hub.py` で、受信 1 通（batch なら外側の 1 通）を DO の `webSocketMessage()` 1 回として数えている。変更後の ping は Cloudflare では自動応答になるので、DO 呼び出しから除いている。req = DO 呼び出し ÷ 20。

| シナリオ | 人数 | 変更前 DO呼び出し/分 | 変更後 DO呼び出し/分 | 変更前 req/分 | 変更後 req/分 | 削減率 | 変更後の内訳/分 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| デスクトップ放置 | 2 | 2,320 | 116 | 116.0 | 5.8 | 95% | ping 4, presence 116 |
| デスクトップ放置 | 3 | 3,474 | 174 | 173.7 | 8.7 | 95% | ping 6, presence 174 |
| デスクトップ放置 | 4 | 4,636 | 230 | 231.8 | 11.5 | 95% | ping 8, presence 230 |
| デスクトップ操作中（1人がドラッグ） | 2 | 4,032 | 1,822 | 201.6 | 91.1 | 55% | batch 20, ping 4, presence 96, xform 1706（batch の中身 40） |
| デスクトップ操作中（1人がドラッグ） | 3 | 5,190 | 1,848 | 259.5 | 92.4 | 64% | batch 42, ping 6, presence 130, xform 1676（batch の中身 84） |
| デスクトップ操作中（1人がドラッグ） | 4 | 6,342 | 1,934 | 317.1 | 96.7 | 70% | batch 20, ping 8, presence 210, xform 1704（batch の中身 40） |
| VR（1人が動きながらドラッグ、他は立っているだけ） | 2 | 4,018 | 2,650 | 200.9 | 132.5 | 34% | batch 570, ping 4, presence 940, xform 1140（batch の中身 1140） |
| VR（1人が動きながらドラッグ、他は立っているだけ） | 3 | 5,162 | 3,010 | 258.1 | 150.5 | 42% | batch 572, ping 6, presence 1304, xform 1134（batch の中身 1144） |
| VR（1人が動きながらドラッグ、他は立っているだけ） | 4 | 6,352 | 3,384 | 317.6 | 169.2 | 47% | batch 572, ping 8, presence 1676, xform 1136（batch の中身 1144） |

- 放置中に残っているのは heartbeat（1 人 1 回/秒）と ping だけ。2 人で放置した場合、無料枠に達するまでの時間は **約 14 時間 → 約 12 日分**になる（5.8 リクエスト/分）。
- デスクトップで操作中に残っているのは、ほとんどがドラッグしている人の xform（30 Hz）。ここを減らすなら案 E（§4）。
- VR では、動いている人の presence と xform が同じ tick に重なることが多く、batch で 1 通にまとめられている（batch 570 通の中身が 1,140 通）。立っているだけの人の presence は、1 人約 1,150 → 約 240 回/分（モデルの揺れ 1 cm の場合）。

### 6.2 モデル（`tools/relay_load_sim.py --seconds 120`）

人数・状況を広めにとった見積り。古いアドオンが混ざっている場合と、リレーをまだ更新していない場合も含む。

(1 分あたり。120 秒シミュレーションの換算。req = DO 課金リクエスト、上限 = 無料枠 10 万 req/日 を使い切るまでの連続時間)

| シナリオ | 変更前 受信msg/分 | 変更後 受信msg/分 | 変更前 req/分 | 変更後 req/分 | 削減率 | 変更前 上限まで | 変更後 上限まで |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1人・デスクトップ放置 | 1,004 | 2 | 50.2 | 0.0 | 100% | 33.2 h | ∞ |
| 2人・デスクトップ放置 | 2,016 | 122 | 100.8 | 5.8 | 94% | 16.5 h | 287.4 h |
| 2人・デスクトップ操作中（1人がドラッグ） | 3,419 | 1,502 | 170.9 | 74.9 | 56% | 9.8 h | 22.3 h |
| 2人・VR放置（全員VRで立っているだけ） | 2,020 | 780 | 101.0 | 38.8 | 62% | 16.5 h | 43.0 h |
| 2人・VR操作中（1人がVRで動きながらドラッグ） | 3,418 | 2,381 | 170.9 | 118.8 | 30% | 9.8 h | 14.0 h |
| 3人・デスクトップ放置 | 3,024 | 183 | 151.2 | 8.8 | 94% | 11.0 h | 189.4 h |
| 3人・デスクトップ操作中（1人がドラッグ） | 4,421 | 1,558 | 221.1 | 77.5 | 65% | 7.5 h | 21.5 h |
| 3人・VR放置（全員VRで立っているだけ） | 3,020 | 1,174 | 151.0 | 58.3 | 61% | 11.0 h | 28.6 h |
| 3人・VR操作中（1人がVRで動きながらドラッグ） | 4,415 | 2,766 | 220.8 | 137.9 | 38% | 7.5 h | 12.1 h |
| 4人・デスクトップ放置 | 4,036 | 244 | 201.8 | 11.7 | 94% | 8.3 h | 142.5 h |
| 4人・デスクトップ操作中（1人がドラッグ） | 5,434 | 1,622 | 271.7 | 80.6 | 70% | 6.1 h | 20.7 h |
| 4人・VR放置（全員VRで立っているだけ） | 4,022 | 1,551 | 201.1 | 77.0 | 62% | 8.3 h | 21.6 h |
| 4人・VR操作中（1人がVRで動きながらドラッグ） | 5,418 | 3,165 | 270.9 | 157.8 | 42% | 6.2 h | 10.6 h |
| 2人・デスクトップ放置・v0.12.1が1人混在（新リレー） | 2,016 | 1,068 | 100.8 | 53.1 | 47% | 16.5 h | 31.4 h |
| 3人・デスクトップ放置・v0.12.1が1人混在（新リレー） | 3,024 | 1,130 | 151.2 | 56.1 | 63% | 11.0 h | 29.7 h |
| 2人・VR操作中・新アドオン＋旧リレー（未デプロイ） | 3,418 | 2,798 | 170.9 | 139.9 | 18% | 9.8 h | 11.9 h |

- **v0.12.1 が 1 人混ざっていると、その人の分（約 1,000〜1,160/分）は減らない。** みんなが更新するほど効果が上がる。
- 新しいアドオンを今の本番リレー（未デプロイ）につないだ場合も、presence の削減は効く。batch と ping の自動応答の分だけ効果が小さくなる。

### 6.3 ローカルの Worker での確認

`wrangler dev`（手元の PC だけで動く Cloudflare のシミュレータ。本番やアカウントには一切つながない）で、変更後の `index.js` を動かして確認した：

- `{"t":"ping"}` を 3 回送ると pong が 3 回返り、`webSocketMessage()` は 1 回も呼ばれなかった（ログで確認）→ 自動応答が効いている
- `batch` の中の ping は `webSocketMessage()` で処理されて、pong が返る
- unit テストと live テスト（下）が、ローカルの Worker に対してもすべて通った

---

## 7. 後方互換

### 確認済み（自動テスト）

| 組み合わせ | 確認内容 | テスト |
| --- | --- | --- |
| 新リレー（Python）＋ 新ホスト・新ゲスト・**v0.12.1 ゲスト**（3 人） | 参加、Cube の移動（新 → 両方、v0.12.1 → 両方）、放置 6 秒の間にどちらの側でも presence が 3 秒以上途切れない（最大 1.05 秒）、選択の表示、新ゲストが抜けて再参加 → 残りの 2 人に peer_leave / peer_join、シーンの再受信 | `test_relay_traffic.py live` |
| **v0.12.1 リレー**（Python）＋ 上と同じ 3 人 | 上と同じ（新しいアドオンは batch を使わない） | `test_relay_traffic.py live-oldrelay` |
| **新 Cloudflare Worker（wrangler dev）**＋ 上と同じ 3 人 | 上と同じ | `COLLAB_RELAY_URL=ws://127.0.0.1:8787 … live` |
| **変更前の Cloudflare Worker（wrangler dev。今の本番と同じコード）**＋ 上と同じ 3 人 | 上と同じ。新しいアドオンは batch を使わず、presence の削減だけが効く | 同上（変更前の `index.js` で起動） |
| 新リレー（Python・Worker）＋ 生のプロトコルで話すクライアント 4 人 | 4 人参加、batch の中身が 1 通ずつ・順番どおり・`from` 付き・`to` どおりに届く、中に入れた hello / batch は無視される、**受け手は batch を一度も受け取らない**（= v0.12.1 と同じものが届く）、ping/pong、存在しない部屋、hello 前の送信、ゲストとホストの切断・再接続 | `test_relay_traffic.py unit`（Worker でも実行） |
| 既存テスト一式 | 下の §7.1 | 既存 |

### 7.1 テスト結果

Python 3.11 + `bpy` 5.0.1（Linux・ヘッドレス）で、変更前（main）と変更後（このブランチ）に同じテストを流した。

| テスト | 変更前 | 変更後 |
| --- | --- | --- |
| `test_sync.py` relay / ws / wss / direct | 4/4 合格 | 4/4 合格 |
| `test_data` `test_grease_pencil` `test_materials` `test_material_nodes` `test_pose` `test_armature` `test_skinning` `test_glb` `test_undo`（relay） | 9/9 合格 | 9/9 合格 |
| `test_relay_default` `test_update_check` `test_updater` `test_vr_studio` | 4/4 合格 | 4/4 合格 |
| `test_relay_traffic.py`（unit + live + live-oldrelay、新規） | — | 合格 |
| `test_relay_traffic.py unit` / `live`：**新 Worker**（wrangler dev） | — | 合格 |
| `test_relay_traffic.py live`：**変更前の Worker**（wrangler dev）＋ 新アドオン・v0.12.1 混在 | — | 合格 |
| `test_sync.py relay`：**新 Worker**（wrangler dev） | — | 合格 |

実行していないもの：`test_glb_real.py`（実際の GLB ファイルを指定する手動テスト）、`test_vr_studio_release.py`（VR Studio の配布 zip を確認するテスト。今回は関係ない）、`test_relay_default.py live`（本番の公開リレーにつなぐので、ルールに従って実行していない）。
`test_vr_studio.py` は、この環境に Freebird 本体がないため unit の部分だけが動いた。変更前も同じ。

### 未確認（実機・本番でしか確かめられないこと）

- **本物の Cloudflare 上での動き**：自動応答が本番でも DO を起こさないこと、ダッシュボードのリクエスト数が実際に減ること。（`wrangler dev` は本番と同じ workerd で動くが、課金の数え方までは再現できない）
- **本物の VR ヘッドセット**：bpy にはヘッドセットがないので、VR の頭・手の動きはモデルで作った。実際の手の震えの大きさによっては、不感帯 2 mm で減る量が変わる。相手の手や頭の表示が「カクつかない／遅れない」かは、実機で見てもらう必要がある。
- **Windows の Blender GUI 上**（タイマーの間隔が `bpy` モジュールと違う可能性がある）、wss 経由で日本と海外のような遅延が大きい場合の heartbeat の届き方。
- v0.12.0 以前のアドオン：ネットワーク部分（`session.py` / `link.py` / `hub.py` の presence と ping）は v0.12.1 と同じなので、同じように動くはず。テストしたのは v0.12.1 だけ。
- `relay/standalone/app.py`・`relay/hf_space/app.py` のリレー：コードを見て「batch を返さないので古いリレーとして扱われる」ことを確認しただけで、実際には動かしていない。

---

## 8. 効果が出る順番（大事）

| ちきんがやること | 減る量 |
| --- | --- |
| リレーだけデプロイ（みんな v0.12.1 のまま） | ping の分だけ。**1% 未満**。ただし 1 人で待っている時に DO が休めるようになる |
| アドオンを更新して配布（リレーは古いまま） | presence の削減は効く（2 人放置で約 94% 減）。batch は使われない |
| 両方 | 表 §6.1 のとおり |

今回はルールに従って、アドオンのバージョン番号と配布 zip は変えていない。**効果を出すには、ちきんが新しいバージョンとしてリリースする必要がある。**

---

## 9. Cloudflare へのデプロイと確認の手順（ちきんの操作）

デプロイは本番に反映されるので、このブランチを main にマージした後にちきんが行う。

### 9.1 デプロイ前（任意・手元だけで確認）

```
cd FreebirdCollabo\relay\cloudflare
npm install
npx wrangler dev --port 8787
```
別のコマンドプロンプトで：
```
python tests\test_relay_traffic.py unit                       ← 普通の Python で OK（Blender 不要）
set COLLAB_RELAY_URL=ws://127.0.0.1:8787
python tests\test_relay_traffic.py unit                       ← 手元の Worker に対して
```
`UNIT PASS` が出れば OK。`wrangler dev` は Ctrl+C で止める。

### 9.2 デプロイ前の数字をメモしておく

1. <https://dash.cloudflare.com/> → **Workers & Pages** → `freebird-relay` → **Metrics**（または **Durable Objects** → `Hub`）
2. 「Requests」と「Duration」の、直近 24 時間や 7 日間の数字をスクリーンショットしておく

### 9.3 デプロイ

```
cd FreebirdCollabo\relay\cloudflare
git pull
npx wrangler deploy
```
`wrangler.toml` は変えていないので、Durable Object の migration は聞かれないはず（聞かれたら止めて相談してほしい）。

### 9.4 デプロイ後すぐの確認（5 分）

1. ブラウザで `https://freebird-relay.chickenos.workers.dev/` → `freebird-collab relay ok`
2. `https://freebird-relay.chickenos.workers.dev/stats` → `{"sockets":0,"rooms":0}` のような JSON が出る
3. **v0.12.1 のアドオン（今配っている zip）**で：Preferences → Check Relay → `OK xxx ms`
4. **v0.12.1 どうし**で Create Room → Join → Cube を動かして反映される（今までどおり）
5. このブランチのアドオンを入れた PC と v0.12.1 の PC を混ぜて：
   - お互いの Cube の移動が反映される
   - 相手の選択（枠）とツール名のラベルが表示されたままになる（**30 秒ほど何もしないで放置しても消えない**こと）
   - VR の人がいれば：頭・手・レイが表示されたまま、動きがカクつかない
   - 片方が Leave → もう一方の N パネルから名前が消える → もう一度 Join → シーンが読み込まれる
6. N パネル下の `tx … rx …`：このブランチのアドオンは、放置中に `tx` の増え方がゆっくりになる（毎秒 1 くらい）

### 9.5 1〜2 日後

Metrics の Requests を 9.2 のメモと比べる。全員が新しいアドオンにするまでは、v0.12.1 の人の分が残る（§8）。

### 戻したい時

```
npx wrangler rollback
```
（またはひとつ前のコミットに戻してから `npx wrangler deploy`）。変更前の Worker は新しいアドオンともそのままつながる（`live-oldrelay` と同じ組み合わせ）。
