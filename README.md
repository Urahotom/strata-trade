# strata-trade

三菱UFJ eスマート証券「kabuステーションAPI」向けの自動売買システム。
既存コードのコピーではなく、公式仕様に基づくスクラッチ実装。

**現在の実装**: P0〜P3 完了（APIクライアント / 注文管理 / リスク / 通知 / データ層 /
戦略 / バックテスト / 1営業日エンジン）。次段階は P4 ペーパートレード。

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
| `strata.strategy` | 戦略プラグイン（シグナル出力のみ・発注権限なし） | P3 完了 |
| `strata.backtest` | 過去データ再生（意図台帳/リスクは運用と同一コード） | P3 完了 |
| `strata.engine` | 1営業日の状態機械（取得→戦略→リスク→注文→監視→終値） | P3 完了 |
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
- P4: ペーパートレード（検証環境）
- P5: 少額実運用

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
