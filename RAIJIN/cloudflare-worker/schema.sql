CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY,
  received_at INTEGER NOT NULL,
  action TEXT NOT NULL,
  ticker TEXT NOT NULL,
  strategy TEXT,
  timeframe TEXT,
  price REAL,
  atr REAL,
  rsi REAL,
  regime TEXT,
  ivr REAL,
  vol_ratio REAL,
  forwarded INTEGER DEFAULT 0,
  forward_status INTEGER,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_action ON signals(action);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
