# strata-trade

三菱UFJ eスマート証券「kabuステーションAPI」向けの自動売買システム。
既存コードのコピーではなく、公式仕様に基づくスクラッチ実装。

**現在の実装**: P0〜P4 実装（APIクライアント / 注文管理 / リスク / 通知 / データ層 /
戦略 / バックテスト / 1営業日エンジン / ペーパートレード配線・CLI）。
P4の運用ゲート（検証環境で2週間無事故）を実行中にして初めてP5へ。

## 設計の原則

1. **ブローカーが真実、DBは意図の台帳** — 発注前に意図 (Intent) をSQLiteへ書き、
   応答喪失時は再送せず突合 (`GET /orders`) で確定させる（二重発注防止）
2. **発注系は自動リトライしない** — リトライは取得系のみに限定
3. **戦略は発注できない** — 戦略はシグナルのみ出力し、リスク・注文層が実行する
4. **テストは既定でオフライン** — 既定の `pytest` はネットワーク不要。
   本番ポート (18080) への接続はテストプロセス全体で遮断
5. **段階的実運用** — バックテスト → ペーパートレード → 少額実取引

## モジュール構成

| パッケージ | 責務 | 段階 |
|---|---|---|
| `strata.api` | kabuステーションAPIクライアント（認証・レート制限・タイムアウト） | P1 完了 |
| `strata.store` | SQLite台帳（intents / events / executions / positions / bars） | P0 完了 |
| `strata.order` | 注文ライフサイクル（Intent Log・状態機械・突合・リコンサイル） | P2 完了 |
| `strata.risk` | 発注前チェック・サイジング・ロスカット・キルスイッチ | P2 完了 |
| `strata.notify` | SMTPメール通知（INFO/ALERT/CRITICAL） | P2 完了 |
| `strata.data` | 日足（J-Quants / yfinance）・品質バリデータ・出典突合 | P3 完了 |
| `strata.strategy` | 戦略プラグイン（MAクロス/RSI/ボリンジャー/ドンチアン・発注権限なし） | P3/P6 |
| `strata.backtest` | 過去データ再生＋**トレード単位台帳CSV・シグナル有効性レポート** | P3/P6 |
| `strata.engine` | 1営業日の状態機械＋本番配線・ペーパートレードCLI | P3/P4 完了 |
| `strata.sim` | フェイクkabuステーションサーバ（開発・テスト専用） | P0 完了 |

## セットアップ

```powershell
uv venv --python 3.12
uv sync --all-extras
uv run pytest            # オフラインテスト（既定）
uv run ruff check .
uv run mypy
```

> **OneDrive 同期フォルダ内の場合の注意**: uv のハードリンクが失敗することが
> あります（os error 396）。その場合は `$env:UV_LINK_MODE = "copy"` を設定して
> `uv sync` し直してください。

各フェーズの動作確認手順（自動テスト＋手動シナリオ）は **`docs/VERIFY.md`** にまとめています。

## 設定

`config/config.example.toml` を `config/config.toml` にコピーして編集。
APIパスワードは環境変数のみ:

```powershell
$env:STRATA_API__PASSWORD = "..."
```

- 環境: `api.environment = "test"`（ポート18081・検証環境）/ `"production"`（18080・本番）
- 設定ファイルの切替: 環境変数 `STRATA_CONFIG_FILE`

## 安全装置

- テストからの本番ポート (18080) 接続を `tests/conftest.py` でソケット層遮断
- `live` マーカーのテストは `STRATA_ALLOW_LIVE=1` のときのみ検証環境 (18081) へ接続可
- 発注パラメータは送信前に pydantic で検証（P1）
- 起動時リコンサイルでオーファン建玉を検知したら全発注停止（P2）

## 開発ロードマップ

- **P0 (完了)**: リポジトリ・設定・DBスキーマ・状態機械定義・フェイクサーバ・CI
- **P1 (完了)**: APIクライアント（トークン管理・レート制限・障害注入テスト）
- **P2 (完了)**: 注文管理＋リスク管理＋メール通知（フェイクサーバ統合テスト）
- **P3 (完了)**: データ層（J-Quants/yfinance）・MAクロス戦略・バックテスト・1営業日エンジン
- **P4 (実装済)**: ペーパートレード配線（`strata.engine run/reconcile/status`）・台帳デモ。
  検証環境での2週間無事故ゲートはここから運用
- P5: 少額実運用
- **P6 (実装済)**: シグナル追加（RSI/ボリンジャー/ドンチアン）とトレード単位
  有効性レポート（`scripts/backtest_report.py`）

## 使い方（P3で揃う最小フロー）

```python
from strata.backtest import BacktestRunner, BacktestSettings, FillPolicy
from strata.config import RiskSettings, WatchlistEntry, load_settings
from strata.data import BarRepository, make_provider  # 実データ取得時
from strata.strategy import MovingAverageCross

settings = load_settings()
strategy = MovingAverageCross(fast=5, slow=25)
universe = [WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)]

# バックテスト（ブローカーだけ仮定、意図台帳/リスクは運用と同一）
runner = BacktestRunner(
    strategy=strategy,
    universe=universe,
    bars=bars,  # {symbol: [DailyBar]}
    risk_settings=RiskSettings(),
    settings=BacktestSettings(initial_cash=1_000_000, fill=FillPolicy.NEXT_OPEN),
)
result = runner.run()
print(result.metrics)  # total_return / max_drawdown / win_rate ...
```

1営業日の実運用は `DailyEngine`（`provider` / `strategy` / `manager` / `risk` を注入し
`run_day(date)` を呼ぶ）で、当日バー欠損時は自動中断します。

> データは必ず品質バリダを通します。当日バー欠損（`halt_when_today_bar_missing`）、
> `high < low` 等の異常値は採用せず取引を中断します。

## シグナル有効性レポート（P6）

「どのシグナルが儲かるのか」をトレード単位で検証する。MA/RSI/ボリンジャー/
ドンチアンの4戦略を同一相場でバックテストし、**エントリー根拠・銘柄・
エグジット根拠別の勝率・期待値・プロフィットファクタ**とトレード単位CSVを出力する:

```powershell
uv run python scripts/backtest_report.py                    # 合成相場で即実行
uv run python scripts/backtest_report.py --real 8306,9984   # 実データ（要データキー）
# 出力: var/reports/trades_<戦略>.csv と var/reports/report.md
```

エンジンの戦略切替は `config.toml` の `[engine] strategy = "rsi_reversion"` など
（パラメータ調整はバックテスト側で行い、運用は設計標準値で回す）。

## ペーパートレード（P4: 検証環境で1営業日を回す）

`api.environment = "test"`（ポート18081）でkabuステーションAPIの検証環境に接続し、
1日1プロセスで `act_time`（既定09:10）にシグナル→発注、`close_time`（既定15:20）まで
約定監視・ロスカット、終値後にスナップショットと日次サマリを出します。

```powershell
# 0. config/config.toml に [engine] と [[watchlist]] を用意（config.example.toml 参照）
#    J-Quants キー: $env:STRATA_DATA__JQUANTS__APP_ID = "..."

# 1. 点検（発注は一切しない: 認証→起動時リコンサイル→報告のみ）
uv run python -m strata.engine reconcile

# 2. 台帳のぞき見（API接続なし）
uv run python -m strata.engine status

# 3. 本番同款の1営業日（寄前に起動し act_time まで待機する）
uv run python -m strata.engine run
```

- **毎営業日 08:45 に `run` を起動するタスクスケジューラタスク**を想定（常駐しない）
- 認証失敗・オーファン建玉検知なら発注せずに終了（終了コード1）
- 当日の中断理由は `run` の終了コードと日次サマリ通知で判定する

### 台帳込みデモ（実アプリ不要）

発注→約定→意図台帳への記録まで、フェイクkabuステーションで一気に見る:

```powershell
# ターミナル1
uv run python -m strata.sim
# ターミナル2
uv run python scripts/demo_sim.py        # 買い約定→売り戻しまで見せてくれる
```

## 手動で触って確認（フェイクkabuステーション）

実アプリ不要で、注文ライフサイクルを対話的に検証できます。

```powershell
# ターミナル1: フェイクサーバを起動（既定 127.0.0.1:18081 / パスワード testpass）
uv run python -m strata.sim

# ターミナル2: クライアントを検証環境設定に向けて発注まで流す
$env:STRATA_API__PASSWORD = "testpass"
uv run python -c "from strata.config import load_settings; from strata.api.client import KabuClient; from strata.api.models import *; c=KabuClient(load_settings()); print(c.authenticate()); print(c.register([('8306',1)])); print(c.sendorder(SendOrderRequest(symbol='8306', side=OrderSide.BUY, qty=100, price_type=PriceType.MARKET)))"
```

サーバ側でシナリオをその場で注入する制御API（公式仕様外・認証不要）:

```powershell
curl http://127.0.0.1:18081/kabusapi/_sim/state                       # 状態ダンプ
curl -X POST http://127.0.0.1:18081/kabusapi/_sim/finalize -d '{\"order_id\":\"...\",\"qty\":100,\"price\":1000}'  # 約定させる
curl -X POST http://127.0.0.1:18081/kabusapi/_sim/reject    -d '{\"code\":21}'          # 次発注を買付可能額不足で拒否
curl -X POST http://127.0.0.1:18081/kabusapi/_sim/drop_next                             # 次発注の応答喪失（突合検証）
```

> ポートを変える場合は `--port` と同時にクライアント側でも
> `$env:STRATA_API__PORT_OVERRIDE` を設定してください。

## ライセンス

[MIT](LICENSE) (c) 2026 Urahotom

本ソフトウェアは独立実装です。設計調査の段階で stocktrading（CC BY-NC 4.0）の
公開資料から改善点を参考にしましたが、**ソースコードは一切再活用していない**
独自実装です（参考箇所は `docs/DESIGN.md` §5 に記録）。

> 本ソフトウェアは投資助言ではありません。取引に起因する損害について
> 作者はいかなる責任も負いません（MITの免責条項参照）。実口座での運用前に
> `docs/VERIFY.md` の検証手順をすべて完了させてください。
