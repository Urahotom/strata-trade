-- 0001: 初期スキーマ
-- 台帳の中心は order_intents（Write-Ahead Intent Log）。
-- ブローカー応答・約定・建玉はすべて突合結果として別途保持する。

CREATE TABLE order_intents (
    intent_id      TEXT PRIMARY KEY,                -- アプリ発行のUUID（冪等キー）
    trade_date     TEXT NOT NULL,                   -- 対象営業日 YYYY-MM-DD (JST)
    created_at     TEXT NOT NULL,                   -- ISO8601
    updated_at     TEXT NOT NULL,                   -- ISO8601
    symbol         TEXT NOT NULL,
    market         INTEGER NOT NULL CHECK (market IN (1, 3, 5, 6, 9, 27)),
    side           TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty            INTEGER NOT NULL CHECK (qty > 0),
    price_type     TEXT NOT NULL CHECK (price_type IN ('MARKET', 'LIMIT', 'AT_OPEN', 'AT_CLOSE')),
    price          REAL NULL CHECK (price IS NULL OR price > 0),
    -- P0/P1 は現物 (1) のみ。信用 (2,3) 拡張時に CHECK を緩和する
    cash_margin    INTEGER NOT NULL DEFAULT 1 CHECK (cash_margin = 1),
    state          TEXT NOT NULL CHECK (state IN (
                       'PLANNED', 'SENT', 'ACK', 'WORKING', 'PART_FILLED',
                       'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED')),
    order_id       TEXT NULL,                       -- ブローカー発行 OrderId
    reject_code    INTEGER NULL,
    reject_message TEXT NULL,
    request_json   TEXT NULL,                       -- 発注リクエスト全文（監査用）
    response_json  TEXT NULL                        -- 最終応答全文（監査用）
);

CREATE INDEX idx_intents_trade_date ON order_intents (trade_date);
CREATE INDEX idx_intents_state ON order_intents (state);
CREATE UNIQUE INDEX idx_intents_order_id ON order_intents (order_id)
    WHERE order_id IS NOT NULL;

-- 追記専用イベントログ（状態遷移・応答喪失・突合結果の監査証跡）
CREATE TABLE order_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id   TEXT NOT NULL REFERENCES order_intents (intent_id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT NULL                            -- JSON
);

CREATE INDEX idx_events_intent ON order_events (intent_id);

-- 約定（ブローカー /orders の ExecutionID ベース。突合でUpsert）
CREATE TABLE executions (
    execution_id   TEXT PRIMARY KEY,
    intent_id      TEXT NOT NULL REFERENCES order_intents (intent_id),
    order_id       TEXT NULL,
    trade_date     TEXT NOT NULL,
    traded_at      TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    market         INTEGER NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price          REAL NOT NULL CHECK (price >= 0),
    qty            INTEGER NOT NULL CHECK (qty > 0),
    commission     REAL NOT NULL DEFAULT 0,
    commission_tax REAL NOT NULL DEFAULT 0
);

CREATE INDEX idx_executions_intent ON executions (intent_id);
CREATE INDEX idx_executions_trade_date ON executions (trade_date);

-- 建玉スナップショット（起動時リコンサイルの結果。/positions 由来）
CREATE TABLE positions_snapshot (
    captured_at TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    market      INTEGER NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    hold_qty    INTEGER NOT NULL CHECK (hold_qty >= 0),
    avg_price   REAL NULL,
    raw_json    TEXT NULL,
    PRIMARY KEY (captured_at, symbol, market, side)
);

-- 日足バー（出典別。突合用に source を含む）
CREATE TABLE bars_daily (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL CHECK (length(date) = 10),  -- YYYY-MM-DD (JST)
    open   REAL NOT NULL CHECK (open >= 0),
    high   REAL NOT NULL CHECK (high >= 0),
    low    REAL NOT NULL CHECK (low >= 0),
    close  REAL NOT NULL CHECK (close >= 0),
    volume INTEGER NOT NULL CHECK (volume >= 0),
    source TEXT NOT NULL CHECK (source IN ('jquants', 'yfinance', 'manual')),
    PRIMARY KEY (symbol, date, source),
    CHECK (high >= low)
);

-- 銘柄マスタ（/symbol API + 外部データ突き合わせ）
CREATE TABLE symbols (
    symbol            TEXT PRIMARY KEY,
    market            INTEGER NULL,
    name              TEXT NULL,
    trading_unit      INTEGER NOT NULL DEFAULT 100 CHECK (trading_unit > 0),
    price_range_group TEXT NULL,
    updated_at        TEXT NOT NULL
);
