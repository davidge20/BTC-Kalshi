-- Dashboard SQLite schema (v1)
--
-- Design goals:
-- - Fast queries for Streamlit UI
-- - Preserve raw events for forward compatibility
-- - Derive normalized tables for the common views
--
-- All timestamps are UTC ISO8601 strings (ts_utc).

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Raw append-only event store (ingestion target)
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  event TEXT NOT NULL,
  run_id TEXT,
  event_ticker TEXT,
  market_ticker TEXT,
  order_id TEXT,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_event_ticker_ts ON events(event_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_market_ticker_ts ON events(market_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_order_id_ts ON events(order_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_run_id_ts ON events(run_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_run_id_event_ticker_ts ON events(run_id, event_ticker, ts_utc);

-- Decision context / ladder rows (candidate + skip)
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  run_id TEXT,
  event_ticker TEXT NOT NULL,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  strike REAL,
  price_cents INTEGER,
  fee_cents INTEGER,
  p_model REAL,
  implied_q_yes REAL,
  edge_pp REAL,
  ev REAL,
  spread_cents INTEGER,
  top_size REAL,
  minutes_left REAL,
  spot REAL,
  sigma_blend REAL,
  source TEXT,
  kind TEXT NOT NULL  -- "candidate" or "skip"
);

CREATE INDEX IF NOT EXISTS idx_candidates_event_ts ON candidates(event_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_candidates_market_ts ON candidates(market_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_candidates_ts ON candidates(ts_utc);
CREATE INDEX IF NOT EXISTS idx_candidates_run_event_ts ON candidates(run_id, event_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_candidates_run_market_ts ON candidates(run_id, market_ticker, ts_utc);

-- Orders and lifecycle events
CREATE TABLE IF NOT EXISTS order_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  run_id TEXT,
  order_id TEXT,
  client_order_id TEXT,
  event_ticker TEXT,
  market_ticker TEXT,
  side TEXT,
  action TEXT,
  status TEXT,
  price_cents INTEGER,
  count INTEGER,
  remaining_count INTEGER,
  delta_fill_count INTEGER,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_events_order_ts ON order_events(order_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_order_events_market_ts ON order_events(market_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_order_events_ts ON order_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_order_events_run_order_ts ON order_events(run_id, order_id, ts_utc);

-- Fills (normalized from entry_filled / scale_in_filled / exit_filled)
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  run_id TEXT,
  event_ticker TEXT,
  market_ticker TEXT,
  order_id TEXT,
  side TEXT,
  fill_kind TEXT NOT NULL,   -- entry/scale_in/exit
  count INTEGER NOT NULL,
  price_cents INTEGER,
  fee_cents INTEGER,
  edge_pp REAL,
  pnl_total REAL,
  pnl_per_contract REAL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts_utc);
CREATE INDEX IF NOT EXISTS idx_fills_market_ts ON fills(market_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_fills_event_ts ON fills(event_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_fills_run_ts ON fills(run_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_fills_run_event_ts ON fills(run_id, event_ticker, ts_utc);

-- Positions snapshots (best-effort; not all runs emit these)
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  run_id TEXT,
  event_ticker TEXT,
  market_ticker TEXT,
  side TEXT,
  position_count INTEGER,
  avg_price_cents REAL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions(ts_utc);
CREATE INDEX IF NOT EXISTS idx_positions_event_ts ON positions(event_ticker, ts_utc);

-- PnL snapshots (from event_settled / bot_shutdown / exit_filled where available)
CREATE TABLE IF NOT EXISTS pnl (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  run_id TEXT,
  event_ticker TEXT,
  market_ticker TEXT,
  realized REAL,
  unrealized REAL,
  total REAL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pnl_ts ON pnl(ts_utc);
CREATE INDEX IF NOT EXISTS idx_pnl_event_ts ON pnl(event_ticker, ts_utc);
CREATE INDEX IF NOT EXISTS idx_pnl_run_ts ON pnl(run_id, ts_utc);
CREATE INDEX IF NOT EXISTS idx_pnl_run_event_ts ON pnl(run_id, event_ticker, ts_utc);

-- System health signals (data freshness, errors, config hash, git commit)
CREATE TABLE IF NOT EXISTS system_health (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  run_id TEXT,
  metric TEXT NOT NULL,
  value_num REAL,
  value_text TEXT,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system_health_ts ON system_health(ts_utc);
CREATE INDEX IF NOT EXISTS idx_system_health_metric_ts ON system_health(metric, ts_utc);
CREATE INDEX IF NOT EXISTS idx_system_health_run_ts ON system_health(run_id, ts_utc);

