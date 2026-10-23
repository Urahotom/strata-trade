# strata-trade 設計仕様書

Version: 0.7 / 更新日: 2026-10-23
対象: 三菱UFJ eスマート証券「kabuステーションAPI」自動売買システム
状態: P0〜P4 実装＋P6 シグナル有効性評価（P4運用ゲートは実行待ち）

---

## 1. 背景と基本方針

既存ソフト (stocktrading) と公式仕様・サンプルを調査し、以下の基本方針で
スクラッチ実装する。**参考コードのコピーは行わない。**

1. **ブローカーが真実、DBは意図の台帳** — 発注前に意図 (Intent) をSQLiteへ
   先書きし、応答喪失時は安易な再送せず突合 (`GET /orders`) で確定させる
2. **発注系は自動リトライしない** — リトライは取得系のみに限定
3. **戦略は発注できない** — 戦略はシグナルのみ出力し、リスク・注文層が実行する
4. **テストは既定でオフライン** — 既定の `pytest` はネットワーク不要。
   本番ポート (18080) への接続はテストプロセス全体で遮断
5. **段階的実運用** — バックテスト → ペーパートレード → 少額実取引

### 1.1 取引範囲（初回リリース）

- **現物 (CashMargin=1) のみ**。信用 (2/3)・先物・オプションは対象外。
- ただし台帳スキーマは `cash_margin` 列を持ち、将来拡張に耐える設計とする。

---

## 2. kabuステーションAPI 仕様調査サマリ（設計反映済み事実）

| 項目 | 仕様 |
|---|---|
| ベースURL | `http://localhost:18080/kabusapi`（本番）/ `:18081`（検証） |
| 認証 | `POST /token` → 以後 `X-API-KEY` ヘッダ。トークンは kabuS終了/ログアウト/再発行で失効。**毎日早朝に強制ログアウト**（利用時間 6:30〜翌6:15）→ 401検知時の再認証が必須 |
| 発注 | `POST /sendorder`（現物・信用）/ `/sendorder/future` / `/sendorder/option`。取消は `PUT /cancelorder` |
| 注文・約定照会 | `GET /orders`（`state=5` で終了、`Details.RecType=8` で約定、`CumQty/OrderQty` で部分約定判定） |
| 建玉 | `GET /positions`（`addinfo` で現在値・評価損） |
| レート制限 | 発注 **5件/秒**（FAQ。YAMLは10件/秒と記述揺れ→安全側3件/秒で実装）、情報/余力/登録 10件/秒（同8件/秒）、歩み値 2件/秒（同1件/秒）。超過は HTTP 429 / Code 4001006 |
| WebSocket | **板情報のみ**（400ms間引き・値更新時のみ・登録50銘柄上限）。約定・注文通知はない → `/orders` ポーリングで実現 |
| 冪等キー | **order_entry_id 相当は存在しない** → 二重発注防止はアプリ側実装が必須 |
| 発注規制 | クロス取引 100313/100314、ワンショット上限 100216/4003001/4003002、空売り価格規制、東証(1)直接発注不可（SOR=9/東証+=27 経由） |
| 応答コード | HTTP + `Code`（4001xxx〜4005xxx）+ 発注 `Result`（0/-1/〜100378）の3層 |

---

## 3. システム構成

```
strata-trade/
├── config/                 # config.example.toml（秘密情報は環境変数のみ）
├── src/strata/
│   ├── config.py           # pydantic-settings + TOML（優先順位: init > env > .env > TOML）
│   ├── errors.py           # アプリ側例外階層（ConfigurationError 他）
│   ├── api/                # ① APIクライアント層（他層から隔離）
│   │   ├── client.py       #   KabuClient（httpx、タイムアウト、リトライ方針）
│   │   ├── token.py        #   トークン管理（401再認証・1回リプレイは取得系のみ）
│   │   ├── ratelimit.py    #   カテゴリ別トークンバッケット
│   │   ├── models.py       #   pydantic リクエスト/レスポンスモデル
│   │   ├── codes.py        #   公式エラーコード辞書（解決済みメッセージ付き）
│   │   └── results.py      #   成否の判別結果型
│   ├── store/              # ⑦ 永続化（SQLite WAL + マイグレーション）
│   ├── order/              # ⑤ 注文管理（Intent Log・状態機械・突合・リコンサイル）P2
│   ├── risk/               # ④ リスク管理（発注前チェック・ロスカット・キルスイッチ）P2
│   ├── engine/             # ⑥ 1営業日のスケジュール状態機械 P3
│   ├── data/               # ② 日足（J-Quants / yfinance アダプタ）P3
│   ├── strategy/           # ③ 戦略プラグイン（シグナル出力のみ）P3
│   ├── backtest/           # ⑨ バックテスト（運用と同じコードパス）P3
│   ├── notify/             # ⑧ SMTPメール通知 P2
│   └── sim/                # ⑩ フェイクkabuステーションサーバ（開発・テスト専用）
└── tests/                  # 単位/シミュレータ/実環境 の3層（TESTING.md 参照）
```

### 3.1 APIクライアント（P1）

- httpx クライアント。**タイムアウト必須**: 接続2s、読み取り 情報5s/発注10s。
- **リトライ方針**:
  - 取得系 (GET): タイムアウト・接続断・429 のみ最大3回、指数バックオフ
  - **発注系 (sendorder/cancelorder): 自動リトライ禁止**。ネットワーク失敗は
    「送信済みか不定」として `NetworkFailure` を返し、注文管理層の突合に委ねる
  - 401 時の自動再認証+リプレイは**取得系のみ**。発注系は再認証するが再送しない
- 発注パラメータは送信前に pydantic でランタイム検証する。
  東証(1)指定は SOR(9) へ読み替え。現物売の `FundType="  "` 等は内部補完する
- 戻り値は例外ではなく結果型で返す（`results.py`）。
  成否・HTTP失敗・API失敗（コード解決済み）・発注リジェクト・通信失敗を区別
- 発注系で応答が破損した場合（`ParseFailure`）も `send_may_have_reached=True` とし、
  「送信済み可能性あり＝再送禁止」の対象に含める
- トークン発行はプロセスキー的なファイルロックで直列化し、二重発行
  （旧トークン失効事故）を抑制する

### 3.2 注文管理（P2 実装済み）

- **Write-Ahead Intent Log**: 発注前に `order_intents`（client intent id、状態=
  `PLANNED`）をコミット → `SENT` に更新して送信
- 送信結果不確定（タイムアウト/接続断/応答破損を含む）: **再送禁止**。
  `GET /orders?symbol` で突合し、到達済みなら `ACK`（OrderId復旧）、
  不在を確かめてから `PLANNED` へ巻き戻し、`retry()` のときだけ再送
  （二重発注ゼロ保証。テストで送信回数=1を保証済み）
- `OrderManager` の公開API: `submit / retry / cancel / reconcile / poll /
  startup_reconcile`。約定は `/orders` ポーリングで拾い `executions` に
  `execution_id` 冪等で記録（WebSocket約定通知は存在しない前提）
- 状態機械（`strata/order/states.py` 実装済み・DB CHECK制約と一致）:

```
PLANNED → SENT → ACK → WORKING → PART_FILLED → FILLED
                 ↘ REJECTED      ↘ CANCELED / EXPIRED
SENT → PLANNED（突合で未到着と確定した場合のみ巻き戻し）
```

- `Result != 0` は必ず `REJECTED` へ記録し、同じ意図の自動再発注を禁止
- 部分約定（`CumQty < OrderQty` のまま `state=5`）: 設定可能ポリシー
  （残余取消 / 指値修整）で処理。既定は残余取消
- 起動時リコンサイル: `/orders`（当日）+ `/positions(addinfo)` とDBを突合。
  DBにない建玉＝オーファンとして**全発注停止+メール通知**（自動解消しない）
- クロス防止: 送信前に未約定反対注文を `/orders?state=1..4` でチェック

### 3.3 リスク管理（P2 実装済み）

- 発注前チェック（`risk/checks.py`、すべて必須）: 1回数量/金額上限、
  同時ポジション数、日次想定ロスカット合計 ≤ `max_total_risk`。
  拒否時は意図すら作成しない
- ポジションサイジング: 許容リスク ÷ ロスカット幅 で株数（単元切り下げ）
- キルスイッチ（`risk/killswitch.py`）: `max_daily_loss` 超過 /
  API通信失敗の連続（既定5回）で作動。作動中は submit/retry/cancel を拒否し、
  解除は手動 `reset()` のみ
- ロスカット: エンジンの `monitor` が参考価格（`price_fn`）でロスカットライン
  割れを検知し成行返済を発注（P3 実装済み。1分足ポーリングの間隔調整は P4）
- `/apisoftlimit` との突合はエンジンから呼ぶため P4 に送り

### 3.4 データ・戦略・エンジン（P3 実装済み）

- 日足は `MarketDataProvider` 抽象 + J-Quants / yfinance 実装（`data/`）。
  両アダプタは httpx transport 注入でオフラインテスト可能。出来高の無い
  出典（J-Quants日次）は volume=0
- 品質バリデータ（`data/validate.py`）: `high<low`・NaN・重複日・日付不正は
  **却下**、出来高0・長期ギャップは警告、当日バー欠損は `missing_today` 報告。
  採用バーのみ `bars_daily` へ Upsert（冪等）。出典間終値突合 `cross_check`
- `Strategy.generate_signals(bars) -> list[Signal]` のみ出力。基底が
  ブローカー引数を取れない設計で発注不可を担保（`strategy/`）
- バックテスト（`backtest/`）: `PaperBroker` が `KabuClient` と同一プロトコルを
  実装し、`OrderManager`/`RiskEngine`/`IntentRepository` は**運用と同一**。
  約定は当日終値気配・NEXT_OPEN/CLOSE選択・買付可能額不足は Result=21 で再現
- エンジン（`engine/DailyEngine`）: 1営業日=1プロセスの状態機械。
  `prepare`（取得→検証→中断判定）→ `act`（戦略→サイジング→意図台帳経由発注）
  → `monitor`（約定同期・日次損失でキルスイッチ・ロスカット成行返済）→
  `finish`（建玉スナップショット＋日次サマリ）。当日バー欠損は設定により中断

### 3.5 永続化スキーマ（P0 実装済み）

| テーブル | 役割 | 要点 |
|---|---|---|
| `order_intents` | 意図台帳（主役） | `intent_id` PK (UUID)、`order_id` 部分ユニーク、state CHECK、数量>0、現物 `cash_margin=1` CHECK |
| `order_events` | 追記専用監査ログ | 意図へ外部キー、状態遷移・応答喪失・突合結果 |
| `executions` | 約定 | `execution_id` PK（ブローカー由来、突合Upsert） |
| `positions_snapshot` | 建玉スナップショット | 起動時リコンサイル結果 |
| `bars_daily` | 日足（出典別） | PK=(symbol,date,source)、`high>=low` CHECK |
| `symbols` | 銘柄マスタ | 単元数量・呼値グループ |

- SQLite WAL・外部キー ON・busy_timeout 5s。マイグレーションは
  `migrations/NNNN_name.sql` を単一トランザクションで1回だけ適用

### 3.6 設定

- `config/config.toml`（git対象外）+ 環境変数 `STRATA_*`（`__` でセクション区切り）
- **秘密情報（APIパスワード・SMTPパスワード）は環境変数のみ**。TOMLには書けない
- `api.environment = test | production` でポートを決打ち（18081/18080）
- レート制限値は設定可能だが公式上限（発注5/情報10/歩み値2 件/秒）を超えられない

### 3.7 通知（P2 実装済み）

- SMTPメール（`notify/mailer.py`）。重大度 INFO（日次サマリ）/ ALERT（約定・
  リジェクト・突合復旧）/ CRITICAL（オーファン建玉・キルスイッチ・API連続障害）
- `Notifier` インターフェース + `InMemoryNotifier`（テスト用）/
  `LoggingNotifier`（フォールバック）。SMTP無効時はログへフォールバックし、
  送信失敗を握って本処理を止めない

### 3.8 ペーパートレード運用（P4 実装済み）

- `engine/service.py` が唯一の本番配線（`build_runtime`）。DB接続・通知・
  キルスイッチ・リスク・OrderManager・DailyEngine を設定から組み立てる
- `PaperTrader.run(営業日)`: 認証 → 起動時リコンサイル → act_time まで待機 →
  prepare/act → close_time まで `monitor_interval_s` 間隔で約定同期とロスカット
  監視 → finish（スナップショット＋日次サマリ）
- **1営業日 = 1プロセス**。日次再利用は OS のタスクスケジューラ（寄前に `run` を
  起動）に委ね、常駐プロセスを持たない
- bootstrap で認証失敗・キルスイッチ作動（オーファン建玉）なら発注せずに中止。
  監視の1周失敗は通知して続行（Ctrl+C では終日処理まで行い、部分約定残余は
  OrderManager の残余取消方針が次回 poll で回収）
- CLI: `python -m strata.engine run | reconcile | status`
  （`reconcile` は発注を一切行わない点検用、`status` は API 接続なしの台帳表示）
- 待機は 5 秒刻み睡眠で、停止シグナルに即反応する。約定ポーリング間隔は
  公式注意書き（1分未満）により 60 秒未満を型で強制

### 3.9 シグナル有効性評価（P6 実装済み）

- 指標は pure 関数（`strategy/indicators.py`: sma/rsi[Wilder]/bollinger[母集団SD]/
  donchian）。**その日までの本数だけで計算し未来参照ゼロ**（バイアス防止）
- 戦略は「状態遷移の当日だけ」シグナルを出す（閾値内である間、連発しない）。
  RSI 売られすぎ復帰 / ボリンジャーバンド復帰 / ドンチアン・ブレイクアウト、
  すべて Strategy 基底で発注権限なし（§1.3 の原則は不変）
- BacktestRunner は約定（executions）に**シグナル由来（戦略名・根拠）を紐付け**、
  FIFO で往復トレードに再構築する（`backtest/trades.py`）。売り先行は不整合として例外
- 有効性レポート（`backtest/report.py`）: トレード単位CSV＋エントリー根拠別・
  エグジット根拠別・銘柄別・年別の勝率/期待値/プロフィットファクタ/平均保有日数。
  総合リターンだけでは見えない「どのシグナルが儲けたか」を判断材料化する
- ベンチマーク（`backtest/benchmark.py`）: 同一期間・同一資金の Buy&Hold
  （銘柄ごと均等加重、リバランスなし、未上場期間の枠は現金）。総リターン/最大DDの
  超過と年別対照表をレポートに添付。**beta を超えたか**を問えない比較は無意味だから
- 定位の限界: バックテストは気配=終値・手数料なしの近似。有効性レポートは
  過去最適化の正当化には使えず、判断材料（別期間での再現確認が必須）

---

## 4. ロードマップとゲート

| Phase | 成果物 | ゲート |
|---|---|---|
| P0 完了 | スキャフォールド、スキーマ、状態機械、フェイクサーバ、CI | 全テスト緑 |
| P1 完了 | APIクライアント + 障害注入テスト | タイムアウト/401/429/応答喪失ケース全通過 |
| P2 完了 | 注文管理+リスク+通知 | 二重発注・再起動・部分約定テスト通過（160テスト緑） |
| P3 完了 | データ+ルール戦略+バックテスト+エンジン | 欠損時中断・異常値・空売り防止・MA戦略稼働を確認（225テスト緑） |
| P4 実装済 | ペーパートレード（検証環境）配線・CLI・台帳デモ | 2週間無事故・障害注入リハ合格（運用ゲートは実行待ち） |
| P5 | 少額実運用 | キルスイッチ実動確認・1か月オーファンゼロ |
| P6 実装済 | シグナル追加（RSI/BB/ドンチアン）と有効性レポート | 4戦略比較のCSV/レポート生成 |
| P6.5 実装済 | Buy&Holdベンチマーク・年別内訳・既定期間2015〜 | ベンチマーク超過と年別対照表の表示（292テスト緑） |

---

## 5. 既存ソフト調査から得た改善点（本設計での強制要件化）

| stocktrading の問題 | 本システムの対策 |
|---|---|
| 発注成功→DB保存が非アトミックで二重発注経路あり | Intent Log 先行書き込み + 突合（§3.2） |
| 発注 `Result!=0` のサイレント無視 | `REJECTED` 終端記録 + 自動再発注禁止 |
| `/positions` 起動時突合なし | 起動時リコンサイル必須（オーファン=全停止） |
| 再起動時の状態消失（メモリ1分足・entry_price） | 状態をDBへ永続化 |
| 約定確認 `State==5` のみ（部分約定 untreated） | RecType/CumQty で終端状態を完全処理 |
| WebSocket recv タイムアウト実質無効 | 5sタイムアウト+再接続後REST補完 |
| トークン取得が起動時1回 | 401再認証（取得系）+ 日次再認証 |
| 通知ゼロ | SMTPメール（§3.7） |
