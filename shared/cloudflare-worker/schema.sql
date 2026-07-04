CREATE TABLE IF NOT EXISTS signal_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    system              TEXT    NOT NULL,
    ticker              TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    price               REAL,
    received_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    forwarded           INTEGER NOT NULL DEFAULT 0,
    forward_status_code INTEGER,
    raw_payload         TEXT
);

CREATE INDEX IF NOT EXISTS idx_signal_log_system    ON signal_log(system);
CREATE INDEX IF NOT EXISTS idx_signal_log_ticker    ON signal_log(ticker);
CREATE INDEX IF NOT EXISTS idx_signal_log_received  ON signal_log(received_at);
CREATE INDEX IF NOT EXISTS idx_signal_log_forwarded ON signal_log(forwarded);
