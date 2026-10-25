# strata-trade フェーズ別動作確認ガイド

Version: 1.0 / 更新日: 2026-10-23
対象: 各フェーズ（P0〜P4）で「何が正しく動けば合格か」を実コマンドで確認する手順書
注意書きのないコマンドはリポジトリルートでの実行。PowerShell 前提。

---

## 0. 共通の準備

```powershell
uv sync --all-extras
uv run pytest            # 全自動テスト緑がすべての前提（現在 251 件）
```

- **自動テストはネットワーク不要**（既定）。ここから先の「手動確認」は
  自動テストが拾えない“体感”を補うためのもの
- フェイクkabuステーションを使う節では事前に起動してターミナルを開いたままにする:

  ```powershell
  uv run python -m strata.sim     # 既定 127.0.0.1:18081 / パスワード testpass
  ```

- 日本語出力が文字化けする端末では `chcp 65001` か `$OutputEncoding` を UTF-8 に
- OneDrive 同期フォルダ内で uv が os error 396 を出したら `$env:UV_LINK_MODE = "copy"`

---

## P0 スキャフォールド（スキーマ・状態機械・安全装置）

| 確認すること | コマンド | 合格条件 |
|---|---|---|
| DBマイグレーション | `uv run pytest tests/unit/test_store.py -q` | 全緑（全テーブル・schema_version） |
| 本番ポート遮断ガード | `uv run pytest tests/unit/test_safety_guard.py -q` | 全緑（18080へ行こうとすると例外） |
| フェイクサーバ起動 | `uv run python -m strata.sim` → `curl.exe http://127.0.0.1:18081/kabusapi/_sim/state` | バナー表示・`{"token_issued":false,...}` |

## P1 APIクライアント（認証・レート制限・タイムアウト）

自動: `uv run pytest tests/unit tests/sim -q`

手動シナリオ（フェイクサーバ起動済み・`$env:STRATA_API__PASSWORD = "testpass"`）:

1. **認証**: `uv run python -c "from strata.config import load_settings; from strata.api.client import KabuClient; print(KabuClient(load_settings()).authenticate())"`
   → `ApiSuccess(data=TokenResult(ResultCode=0, Token='sim-token-...'))`
2. **リジェクト応答**: `curl.exe -X POST http://127.0.0.1:18081/kabusapi/_sim/reject -H "Content-Type: application/json" -d '{\"code\":21}'`
   を打ってから発注 → `ApiFailure(code=21, ...)`（買付可能額不足）が返る
3. **応答喪失→突合**: `curl.exe -X POST http://127.0.0.1:18081/kabusapi/_sim/drop_next`
   → 発注はタイムアウト扱いになるが `GET /orders` には注文が現れる
   （**送信済み可能性ありを再送せず突合で検知**するP2の前提動作）

## P2 注文管理・台帳（意図台帳・二重発注防止・リコンサイル）

自動: `uv run pytest tests/unit/test_order_manager.py tests/sim/test_order_manager_integration.py -q`

手動（**台帳込みデモ**が最も早い）:

```powershell
uv run python scripts/demo_sim.py
```

合格条件:

- 買い意図が `ACK → FILLED`、`約定記録: BUY 100株 @ ... (execution_id=X-...)`
- 売り戻しまでの決済サイクルが完走（意図2件・約定2件が `executions` 表に）
- 重複発注阻止: 同一銘柄同方向の未了意図がある状態でもう一度発注しようとすると
  `DuplicateIntentError`（意図作成前に拒否されることまで unit で保証）

台帳ののぞき見（API接続なし）:

```powershell
uv run python -m strata.engine status
uv run python -c "import sqlite3; [print(r) for r in sqlite3.connect('var/demo_sim.db').execute('SELECT execution_id,side,qty,price FROM executions')]"
```

## P3 データ・戦略・バックテスト・1営業日エンジン

自動: `uv run pytest tests/unit/test_data_validate.py tests/unit/test_backtest.py tests/unit/test_engine.py -q`

手動（**完全オフライン**。ネットワーク不要）:

```powershell
uv run python scripts/demo_offline.py
```

合格条件:

- A) MAクロス: シグナル BUY/SELL、約定が `executions` に計上、win率等の成績表示
- B) 当日バー欠損: `phase: FINISHED`（日終りまでは行く）・**発注意図 0 件**・
  `[取引中断]` の ALERT 通知（`halt_when_today_bar_missing=true` の実動）

実データ接続の確認（J-Quants キーが必要）:

```powershell
$env:STRATA_DATA__JQUANTS__APP_ID = "..."
uv run python -c "from strata.config import load_settings; from strata.data import make_provider, validate_bars; from datetime import date, timedelta; p=make_provider(load_settings().data); bars=p.fetch_daily('8306', date.today()-timedelta(days=30), date.today()); r=validate_bars(bars); print(len(bars), len(r.valid), r.missing_today, r.latest_date)"
```

→ 取得本数・有効本数・`missing_today`（当日が営業日なのに無い場合の中断原因）が見える

## P4 ペーパートレード（1営業日運用）

自動: `uv run pytest tests/unit/test_engine_service.py -q`

### レベル1: フェイクサーバで全工程（実アプリ不要）

```powershell
uv run python -m strata.sim                 # ターミナル1
# ターミナル2:
$env:STRATA_API__PASSWORD = "testpass"
uv run python -m strata.engine reconcile    # 認証→起動時リコンサイル（発注ゼロ）
uv run python -m strata.engine status       # 意図台帳表示
```

合格条件: `reconcile` が `=== 起動時リコンサイル報告 ===` を出し終了コード0。
`status` は当日の意図一覧（なければ0件）。

### レベル2: 検証環境 (18081) への実接続

1. `config/config.toml`（example をコピー）で `api.environment = "test"`、
   `[engine]`・`[[watchlist]]` を設定。環境変数で `STRATA_API__PASSWORD` と
   `STRATA_DATA__JQUANTS__APP_ID`
2. kabuステーションを起動しAPI接続が有効なことを確認
3. 実行順:

```powershell
uv run python -m strata.engine reconcile   # ここで設定・認証を潰し込む
uv run python -m strata.engine run         # act_time まで待機して発注→監視→終日処理
```

- 時刻外の検証は `[engine] act_time`「現在+2分」/`close_time`「現在+10分」に
- 非営業日・当日バー未取得なら **発注せずに中断（終了コード1＋中断理由）**。
  これは安全装置の実動確認であり合格
- 終了コード: `run` 0=完走 / 1=中断、`reconcile` 0=OK / 1=認証失敗・キルスイッチ

### レベル3: 障害注入リハ（P4運用ゲートの合格試験）

フェイクサーバに対するシナリオ。各手順で「自動再送しない・台帳が真実」を体感する:

| # | 注入 | 期待挙動 |
|---|---|---|
| 1 | `_sim/reject {"code":21}` → 発注 | 意図は `REJECTED` 終端・自動再発注なし・ALERTメール/ログ |
| 2 | `_sim/drop_next` → 発注 | 応答喪失→`/orders` 突合で注文復旧（`RECONCILE_FOUND`）・二重発注なし |
| 3 | `_sim/delay {"seconds":3}` → 発注 | タイムアウト→突合。#2と同じ着地 |
| 4 | 発注→`_sim/finalize` 一部約定→poll | 残余自動取消（`CANCEL_REMAINDER_SENT`）→`CANCELED` |
| 5 | DBに意図のない建玉をフェイクに作らせて `reconcile` | **オーファン検知→キルスイッチ作動→発注不可** |
| 6 | `run` 実行中に Ctrl+C | 終日処理（スナップショット＋日次サマリ）まで実行されて終了 |

---

## P6〜P8 シグナル有効性評価（RSI・ボリンジャー・ドンチアン＋レポート＋ベンチマーク＋MDD/投資時間）

自動: `uv run pytest tests/unit/test_indicators.py tests/unit/test_strategy_technical.py tests/unit/test_backtest_trades_report.py tests/unit/test_backtest_attribution.py tests/unit/test_backtest_benchmark.py tests/unit/test_backtest_validity_metrics.py -q`

手動（合成相場・ネットワーク不要）:

```powershell
uv run python scripts/backtest_report.py
```

合格条件:

- 標準出力の先頭に **ベンチマーク Buy&Hold の総return（とDD）**、戦略別に超過pt
- 標準出力に戦略別の trade数・勝率・期待値・PF・総return の表
- `var/reports/trades_<戦略>.csv`（1行=1往復: entry/exit 日付・価格・根拠・保有日数）
- `var/reports/report.md` にエントリー根拠別・エグジット根拠別・銘柄別・
  **年別（戦略 vs Buy&Hold）**の集計と概要のベンチマーク超過行
- （P8）**最大ドローダウン区間（日次評価額・ピーク→底の日付・回復/水没日数、
  B&H比）**、概要の**期待値分解 E=pW−(1−p)L−C**（平均利益・平均損失を
  **金額と買付金額比の両記**）、**年率リターン（CAGR）**、
  **市場への投資時間**（全体と年別の投資時間率・平均エクスポージャー）の各節
- （P8）**実験条件カード**（コマンド・期間・資金・C・パラメータ・実行日）が
  report.md 冒頭に出力される。`--history-end` を固定して2回実行し、出力が
  一致することを以て再現確認とする
- （P8）**期間分割検証**: 学習・調整期間（〜`--split`）と最終評価期間
  （`--split`〜）を各戦略で独立実行し、総return・年率・MDD・期待値・
  投資時間を対照表示（採点対象は評価期間のみ）

実データでの検証（要データ取得。既定期間 2015-01-01〜、`--history-start` で変更可）:

```powershell
uv run python scripts/backtest_report.py --real 8306,9984
```

> レポートは「有効性の判断材料」であり最適化の正当化には使えない（過去最適化の
> 罠）。採用判断は別期間（ウォークフォワード相当の期間分割）で再現すること。
> 2015〜実データでの初回比較結果と解釈の注意点は **`docs/FINDINGS.md`**。

---

## P9 広サンプル walk-forward（30銘柄＋TOPIX ETF B&H）

自動: `uv run pytest tests/unit/test_data_adapters.py -q`（指数コードの
`to_dot_symbol` 非変換を含む）

手動（要ネット初回・以後はキャッシュ）:

```powershell
$env:STRATA_DATA__PROVIDER = "yfinance"
uv run python scripts/backtest_walkforward.py
```

合格条件:

- `var/reports/p9_bars.json` にデータキャッシュ（再現キー＝sha256 を
  FINDINGS に記録）。2回目実行はキャッシュ読み込みで同一結果
- 標準出力と `var/reports/walkforward.md` に評価年ごとの
  「選択パラメータ（学習）/ 評価return / 等額B&H比 / 指数比」マトリクスと
  集計（B&Hに勝った回数・平均超過）
- 条件カード（銘柄数・資金・学習年・評価年・データ期間・キャッシュ名）が
  レポート先頭に出力される

---

## P10 リスクオフ・オーバーレイ検証（vs 素のB&H）

自動: `uv run pytest tests/unit/test_backtest_overlay.py tests/unit/test_data_repair.py -q`

手動（要ネット初回・p9キャッシュ再利用）:

```powershell
$env:STRATA_DATA__PROVIDER = "yfinance"
uv run python scripts/overlay_vs_buyhold.py
```

合格条件:

- `var/reports/overlay.md` に条件カード＋全期間表（総return・年率・MDD・
  水没日数・投資時間・切替/年・vs B&H）＋評価窓表＋コスト感度表
- 素のB&H の投資時間が 100%・切替 0 で整合（オーバーレイ側は未来参照なし:
  判定は当日終値・反映は翌日から）
- 修復データの整合: 1306.T の 2015-01 段差と 2026-03 の 2日スパイクが
  系列に現れない（B&H MDD が 40% 未満に収まる）
- 判定は `docs/FINDINGS.md` F-005（全オーバーレイ敗北・P5 不开設）

---

## P11 ML騰落確率戦略（外部ソフト判断のクリーンルーム再実装・実験）

自動: `uv run --extra ml pytest tests/unit/test_strategy_ml_probability.py -q`
（`ml` extra 未導入なら `pytest.importorskip` で自動スキップ。コアは標準LIBのみ）

手動（P9キャッシュ再利用・ネットワーク不要）:

```powershell
uv run --extra ml python scripts/backtest_ml.py
```

合格条件:

- `var/reports/ml.md` に **A 閾値別の監査表**（実現/含み損益・投資時間・平均Eクスポ・
  期末建玉・vs B&H）／**B ランダム対照40通りに対する return/exp 分位**／**C 予測確率の
  校正表**／**D コスト感度**（片道コスト率を売買名簿額に課す）／**E ラベルhorizon感度**
  （lag=0 非実行系 vs lag=1 実行系）が出力される（F-006 参照）
- **未来参照なし**: 学習は `train_end`（評価年始）より前のデータのみで、**ラベル対象日も
  `train_end` 未満に閉じる**（境界リークなし）。約定整合で `lag=1`（t+1→t+2）が既定。
  評価年は完全アウト・オブ・サンプル。**成績は評価期間のみ**で集計（走査窓で薄めない）
- 判定は `docs/FINDINGS.md` F-006（Headlineは含み益依存・return/expのランダム分位は中央
  以下・校正過信・horizon整合でも優位なし＝選択スキルの証拠なし・P5 不開設）

**ライセンス**: 参照元 hajime-f/stocktrading は CC BY-NC 4.0（非営利）。コードは
コピーせず考え方のみの独立再実装。当戦略は実験ブランチ限りの非営利利用とし、
MIT の `main` へのマージ・実運用は行わない（モジュール先頭注記参照）。

---

## 付録A: 終了コード一覧

| コマンド | 0 | 1 | 2 |
|---|---|---|---|
| `engine run` | 完走 | 取引中断・未完走 | 設定エラー |
| `engine reconcile` | OK | 認証失敗/キルスイッチ作動 | 設定エラー |
| `engine status` | 表示完了 | - | 設定エラー |

## 付録B: 確認に使う SQLite 表

| 表 | 中身 |
|---|---|
| `order_intents` | 意図台帳（状態・OrderId・リジェクト） |
| `intent_events` | 状態遷移・観測イベント（`RECONCILE_FOUND` 等） |
| `executions` | 約定（`execution_id` 一意で冪等） |
| `positions_snapshots` | 起動時/日終りの建玉スナップショット |
| `bars_daily` | 取り込み済み日足（出典付き） |
