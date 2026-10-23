# strata-trade テスト仕様書

Version: 0.7 / 更新日: 2026-10-23
関連: [設計仕様書](./DESIGN.md)

実装状況: §3.1 APIクライアント（P1）、§3.2 注文管理・§3.3 リスク/通知（P2）、
§3.4 データ・戦略・バックテスト・エンジン（P3）すべて **実装済み**。
テストファイル: unit = `test_api_client.py` / `test_sendorder_model.py` /
`test_ratelimit.py` / `test_lockfile.py` / `test_intent_repository.py` /
`test_order_manager.py` / `test_risk.py` / `test_notifier.py` /
`test_data_validate.py` / `test_data_adapters.py` / `test_data_store.py` /
`test_strategy.py` / `test_backtest.py` / `test_engine.py`、
sim = `test_client_integration.py` / `test_order_manager_integration.py`。
発注系の「送信済み可能性」は `NetworkFailure` に限らず、応答破損の
`ParseFailure(send_may_have_reached=True)` も対象に含まれる。

---

## 1. 原則

1. **既定の `pytest` は100%オフラインで完結する**（ネットワークアクセスなし・
   実 kabuステーション不要・発注を伴う処理はすべてモック/フェイク）
2. **テスト中に実注文が出ない構成をコードで担保する**
   - `tests/conftest.py` がテストプロセス全体のソケット層で
     **本番ポート 18080 への接続を遮断**する（遮断時の挙動自体もテスト対象）
   - `live` マーカーのテストは `STRATA_ALLOW_LIVE=1` のときのみ実行され、
     その場合も接続先は検証環境 (18081) に限定。18080 は常時遮断
3. **実環境統合テスト (live) は手動実行のみ**。CI では絶対に走らない
   （`addopts = -m "not live"`）
4. 障害系（タイムアウト・APIエラー・部分約定・再起動・重複発注・データ欠損）を
   正常系と同格以上で持つ
5. カバレッジ目標: `api` / `order` / `risk` はライン+ブランチ 90% 以上

## 2. テスト階層

| 階層 | ディレクトリ | 手法 | 実行 |
|---|---|---|---|
| 単体 | `tests/unit/` | httpx `MockTransport` への応答注入、in-memory/一時DB、偽時計 | 既定 |
| シミュレータ | `tests/sim/` | フェイクkabuステーションサーバ (`strata.sim`)。in-process ASGI またはローカル実HTTP（エフェメラルポート） | 既定 |
| バックテスト | `tests/backtest/` (P3) | 過去データ再生 | 既定 |
| 実環境 | `tests/live/` | 検証環境 (18081) のみ。マーカー+環境変数の二重ゲート | 手動 |

## 3. テストケースマトリクス

### 3.1 APIクライアント（P1）

| ケース | 期待挙動 | 実装先 |
|---|---|---|
| 正常系: 認証・板・注文照会・残高・登録・発注・取消 | 型解決済み成功 | unit + sim |
| 接続タイムアウト（GET） | `NetworkFailure`、3回リトライ後放棄 | unit |
| 読み取りタイムアウト（GET） | 同上 | unit + sim(遅延注入) |
| 接続断・プロトコルエラー | `NetworkFailure(kind=connect/protocol)` | unit + sim |
| **発注応答喪失** | `NetworkFailure`。クライアントは**絶対に自動再送しない** | unit(送信回数=1) + sim(応答喪失注入) |
| HTTP 429（GET） | バックオフ後リトライ | unit |
| HTTP 429（発注） | リトライせず失敗返却 | unit |
| HTTP 401（GET） | 自動再認証 + **1回だけ**リプレイ。再401なら失敗 | unit |
| HTTP 401（発注） | 再認証するが**リプレイしない**（二重発注防止） | unit |
| 不正JSON・想定外フィールド | `ParseFailure` / 未知フィールドは無視して続行 | unit |
| 発注 `Result != 0`（例: 21 可能額不足） | `REJECTED` 相当。コード解決済みメッセージ付き | unit + sim |
| トークン発行失敗（パスワード不正 4001013） | 失敗返却・リトライ対象外 | unit + sim |
| 旧トークン失効（再発行後旧で401） | 401経路から復旧 | sim |
| レート制限: カテゴリ別間隔 | 偽時計で strict に間隔制御される | unit |
| 発注モデル検証: qty<=0 / 成行+価格 / 指値+価格0 | 送信前に pydantic で拒否 | unit |
| 東証(1)指定 | SOR(9) へ読み替え | unit |

### 3.2 注文管理（P2 実装済み）

| ケース | 期待挙動 | 実装先 |
|---|---|---|
| 発注前に意図が DB へコミット済みであること | `PLANNED` 行＋`INTENT_CREATED` イベント | unit |
| 応答喪失 → 突合で到達確認 | 復旧して `ACK`（OrderId回収）。**送信回数=1** | unit + sim(応答喪失注入) |
| 応答喪失 → 突合で不在 | `PLANNED` へ巻き戻し、`retry()` ではじめて再送 | unit + sim |
| 突合自体の通信失敗 | `SENT` のまま据え置き＋`RECONCILE_FAILED` | unit |
| リジェクト後の同一意図再発注 | `retry` 拒否＋ALERT通知。別意図での再挑戦は可 | unit + sim |
| 部分約定のまま作業中 | `PART_FILLED`＋残余取消を**1回だけ**要求 | unit |
| 部分約定のまま state=5（失効） | 約定分確定＋`EXPIRED`＋ALERT | unit |
| poll 2回でも約定二重記録なし | `executions` は `execution_id` 冪等 | unit + sim |
| プロセス再起動（SENT残存） | 起動時リコンサイルで収束、野良注文ゼロ | unit + sim |
| 起動時オーファン建玉 | 全発注停止 + CRITICAL通知 | unit + sim |
| クロス（未約定反対注文あり） | 送信前に阻止（意図すら作らない） | unit |
| 同銘柄同方向の重複 | `DuplicateIntentError`（アクティブ意図がある間） | unit |

### 3.3 リスク管理（P2 実装済み）

- サイジング境界（単元切り捨て、0除算防御）: unit
- 発注前チェック拒否（数量・金額・ポジション数・想定リスク合計・
  リスク幅未設定）→ 意図を作成しない: unit + manager結合 unit
- `max_daily_loss` 超過 → キルスイッチ（以後の発注ゼロ）: unit
- API通信失敗の連続 → キルスイッチ（作動後は送信回数が増えないことまで）: unit
- 通知: 送信関数注入で SMTP 送信なし。無効時フォールバック・
  送信失敗を握らない本処理への影響なし: unit

### 3.4 データ・戦略・エンジン（P3 実装済み）

| ケース | 期待挙動 | 実装先 |
|---|---|---|
| 当日バー欠損 | `missing_today=True`、エンジンが `halt_when_today_bar_missing` で HALTED・発注ゼロ | unit（validate / engine） |
| 异常値 high<low | 却下（`bars_daily` 非採用） | unit |
| 出来高0 / 長期ギャップ | 警告のみで採用 | unit |
| 重複日・日付不正 | 却下 | unit |
| 出典間終値突合 | 許容差内は一致・超過を検出（アダプタ同一性） | unit（cross_check） |
| J-Quants / yfinance 変換 | MockTransport で認証・頁取得・null除去・エラーを再現 | unit |
| MAクロス戦略 | 上抜け BUY / 下抜け SELL / 本数不足は空、戦略が発注手段を持たない | unit |
| PaperBroker | NEXT_OPEN 翌日約定 / CLOSE 当日約定 / 買付可能額不足 Result=21 / 実現損益 | unit |
| Runner が意図台帳を通す | executions が `executions` 表に計上、日跨ぎで同期 | unit |
| ロスカット | 監視価格がライン割りで成行売り・ALERT。日次損失でキルスイッチ | unit（engine） |

### 3.5 フェイクサーバ（strata.sim）自体のテスト

仕様の再現度がテスト品質を決めるため、フェイク自体も回帰テストする:
トークン再発行で旧トークン失効、401 の Code 4001009、必須パラメータ欠き 400、
リジェクトスクリプト、取消で `State=5` + `RecType=6`、応答喪失シナリオ、
**安全ガード（18080遮断）自体のテスト**（`tests/unit/test_safety_guard.py`）。

### 3.6 ペーパートレード運用（P4 実装済み）

| ケース | 期待挙動 | 実装先 |
|---|---|---|
| スケジュール進行 | act_time まで待機→prepare/act→close_time まで間隔監視→finish | unit（疑似時計） |
| 認証失敗 | 発注・リコンサイルせず HALTED（CRITICAL通知） | unit |
| 起動時リコンサイル | 当日日付で実行。オーファン建玉なら bootstrap 中止・発注なし | unit |
| prepare 中断 | act せずに finish（日次サマリに中断理由） | unit |
| act でキルスイッチ | HALTED だが finish（スナップショットとサマリ）まで実行 | unit |
| Ctrl+C | act 前でも終日処理へ移行し、ALERT通知のうえ終了 | unit |
| 監視の一時失敗 | 1周失敗は ALERT のみで次週期へ続行 | unit |
| 設定検証 | HH:MM 形式・close>act・MA期間関係・ポーリング60秒未満を型で強制 | unit |
| 台帳込み一気通貫 | 発注→約定→executions 記録（buy/sell往復） | scripts/demo_sim.py（手動） |

### 3.7 シグナル有効性評価（P6/P7 実装済み）

| ケース | 期待挙動 | 実装先 |
|---|---|---|
| 指標の数学 | RSI 一本調了100/0・境界内、BB 母集団SD・幅ゼロ、ドンチアン高安・本日除外基準 | unit（indicators） |
| 3戦略の状態遷移 | 復帰クロス／ブレイク当日のみシグナル。閾値内連発なし・本数不足は無反応 | unit（technical） |
| トレード再構築 | FIFO対応（分割買い・部分対応・買い残）、売り先行は例外、日数正規化 | unit（trades） |
| 有効性集計 | 勝率・期待値・PF（損失ゼロ=∞）・最大損益・グループ化 | unit（report） |
| CSV/Markdown 出力 | ヘッダー付きCSV・根拠別/銘柄別テーブル・未決済玉の表示 | unit |
| Runner 帰属 | 約定に戦略名と根拠が付き、fills→trades→集計がend-to-endで繋がる | unit（synthetic相場） |
| Buy&Holdベンチマーク | 均等加重・開始前枠は現金・年次リターン・空データゼロ | unit（benchmark） |
| 年別セクション | 決済年グループ集計と B&Hold 年間リターンの対照表を描画 | unit |

## 4. CI 方針

- GitHub Actions: `ruff check` → `ruff format --check` → `mypy`（strict）→ `pytest`
- CI は常にオフライン。`live` はマーカー除外済み
- テストがパスしない構成（例: 18080 遮断の無効化）への変更は即座にCI失敗

## 5. 実行方法

```powershell
uv run pytest            # 既定（unit + sim、オフライン）
uv run pytest -m sim     # フェイクサーバ統合のみ
uv run pytest -m live    # 検証環境（要 STRATA_ALLOW_LIVE=1 + 実 kabuステーション 18081）
```

## 6. 実環境テスト (live) の運用ルール

- 検証環境ポート (18081) への送信のみ許可。**18080 はソケット層で常時遮断**
- 発注を含むテストは `live` + `order_api` マーカーの二重マーカーを要求し、
  README の手順（検証環境の確認表示）を管理者が確認済み場合のみ手動実行する
- 約定した注文は即取消する後始末フィクスチャを必須とする
