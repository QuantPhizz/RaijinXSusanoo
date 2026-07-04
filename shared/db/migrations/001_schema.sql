-- ============================================================================
-- TKO-AGENTS DATABASE SCHEMA v1.1
-- Shared infrastructure for RAIJIN (premium selling) and SUSANOO (long premium)
-- PostgreSQL 15+
--
-- v1.1 (2026-06-27): pdt_counter → intraday_trades. FINRA PDT eliminated
--   2026-06-04; this table is now OBSERVABILITY ONLY, not a gate. Kelly sizer
--   + circuit breakers are the sole frequency governors. v_pdt_slots_remaining
--   retained as a compatibility shim (returns unlimited shape). New canonical
--   view: v_intraday_summary. REJECTED_PDT enum value retained but defunct.
--   This file is the schema-of-record; apply it (then 002) to a fresh DB.
-- ============================================================================

-- Run order: this file is idempotent (IF NOT EXISTS on all objects)

-- ============================================================================
-- ENUM TYPES
-- ============================================================================

DO $$ BEGIN
    CREATE TYPE system_name AS ENUM ('RAIJIN', 'SUSANOO');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE edge_type AS ENUM (
        'VRP',                -- Variance Risk Premium (positive = sell, negative = buy)
        'TERM_STRUCTURE',     -- Calendar spread / term structure slope dislocation
        'SKEW_DISLOCATION',   -- 25Δ put/call skew anomaly
        'DIRECTIONAL',        -- Pure directional momentum
        'EARNINGS_DRIFT'      -- Post-earnings announcement drift
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE trade_status AS ENUM (
        'PENDING',            -- Signal accepted, order not yet submitted
        'SUBMITTED',          -- Order sent to IBKR
        'FILLED',             -- Execution confirmed
        'PARTIAL_FILL',       -- Partially filled (rare for single contracts)
        'CANCELLED',          -- Order cancelled (timeout, slippage breach, etc.)
        'CLOSED_PROFIT',      -- Position closed at profit target
        'CLOSED_STOP',        -- Position closed at stop-loss
        'CLOSED_TIME_STOP',   -- SUSANOO: closed by time-stop protocol
        'CLOSED_WEEKEND',     -- SUSANOO: Friday liquidation
        'CLOSED_EARNINGS',    -- Closed due to earnings exclusion zone
        'CLOSED_CIRCUIT',     -- Closed by circuit breaker
        'CLOSED_MANUAL',      -- Closed via /halt endpoint or manual intervention
        'EXPIRED'             -- Option expired (should never happen with proper management)
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE signal_status AS ENUM (
        'ACCEPTED',           -- Passed all gates, forwarded to risk engine
        'REJECTED_GATE_1',    -- Failed IV regime check
        'REJECTED_GATE_2',    -- Failed VRP check
        'REJECTED_GATE_3',    -- Failed term structure / GEX gate
        'REJECTED_GATE_4',    -- Failed directional catalyst
        'REJECTED_GATE_5',    -- Failed Edge Ledger (edge_magnitude <= threshold)
        'REJECTED_RISK',      -- Passed signal gates, rejected by risk engine
        'REJECTED_PDT',       -- DEFUNCT (FINRA PDT eliminated 2026-06-04). Retained for enum compatibility; never emitted.
        'REJECTED_CAPITAL',   -- Rejected: capital fence exhausted
        'REJECTED_EARNINGS',  -- Rejected: within earnings exclusion zone
        'REJECTED_LIQUIDITY', -- Rejected: bid-ask spread or OI too thin
        'REJECTED_DUPLICATE', -- Rejected: duplicate signal guard
        'REJECTED_CONFLICT'   -- Rejected: same underlying already held by sibling system
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE option_type AS ENUM ('CALL', 'PUT');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE structure_type AS ENUM (
        'BULL_PUT_SPREAD',    -- RAIJIN: sell put, buy lower put
        'BEAR_CALL_SPREAD',   -- RAIJIN: sell call, buy higher call
        'LONG_CALL',          -- RAIJIN (low IVR) or SUSANOO
        'LONG_PUT'            -- RAIJIN (low IVR) or SUSANOO
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ============================================================================
-- TABLE: signals
-- Every incoming webhook signal, whether accepted or rejected.
-- This is the audit trail for signal quality measurement.
-- ============================================================================

CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    system              system_name NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Source payload from TradingView webhook
    ticker              VARCHAR(10) NOT NULL,
    direction           VARCHAR(10) NOT NULL,          -- 'BUY' or 'SELL'
    tv_price            NUMERIC(12,4),                 -- Price at TV alert fire
    tv_atr              NUMERIC(12,4),
    tv_rsi              NUMERIC(8,4),
    tv_regime           VARCHAR(20),                   -- Regime label from Pine Script
    raw_payload         JSONB,                         -- Full webhook JSON for debugging

    -- Gate evaluation results (populated by signal engine)
    ivr_value           NUMERIC(8,4),                  -- IVR at evaluation time
    ivp_value           NUMERIC(8,4),                  -- IVP at evaluation time
    vrp_value           NUMERIC(8,4),                  -- VRP estimate (IV - RV)
    gex_flip_price      NUMERIC(12,4),                 -- FlashAlpha flip price (RAIJIN)
    gex_above_flip      BOOLEAN,                       -- Is underlying above flip?
    term_slope          NUMERIC(8,4),                  -- Front IV / Back IV ratio (SUSANOO)
    skew_25d            NUMERIC(8,4),                  -- 25Δ put/call skew value

    -- Decision
    status              signal_status NOT NULL,
    rejection_reason    TEXT,                           -- Human-readable gate failure detail
    
    -- IS Checkpoint 1 (decision price at signal evaluation)
    decision_price      NUMERIC(12,4),                 -- Mid-price of target option/spread at signal time
    decision_ts         TIMESTAMPTZ                    -- Timestamp of decision price capture
);

CREATE INDEX IF NOT EXISTS idx_signals_system ON signals(system);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_received ON signals(received_at);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);


-- ============================================================================
-- TABLE: edge_ledger
-- Every trade's quantified edge, computed before order submission.
-- The edge gate: if edge_magnitude <= threshold, the trade is blocked.
-- Post-trade, realized_pnl is populated to measure edge capture.
-- ============================================================================

CREATE TABLE IF NOT EXISTS edge_ledger (
    id                  BIGSERIAL PRIMARY KEY,
    trade_id            BIGINT,                        -- FK to trades.id (populated after fill)
    signal_id           BIGINT REFERENCES signals(id),
    system              system_name NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Edge specification (pre-trade)
    ticker              VARCHAR(10) NOT NULL,
    edge_type           edge_type NOT NULL,
    edge_magnitude      NUMERIC(12,4) NOT NULL,        -- Expected $ value of edge
    edge_source         TEXT NOT NULL,                  -- Data provenance string
    edge_confidence     NUMERIC(6,4) DEFAULT 0.5,      -- Bayesian posterior (0-1)

    -- Computation inputs (for audit / reproducibility)
    iv_at_entry         NUMERIC(8,4),                  -- IV used in edge computation
    rv_forecast         NUMERIC(8,4),                  -- RV forecast used
    vega_exposure       NUMERIC(12,4),                 -- Vega of the position
    dte_at_entry        INTEGER,                       -- Days to expiration at entry
    
    -- Post-trade attribution (populated at position close)
    realized_pnl        NUMERIC(12,4),                 -- Actual P&L of the trade
    edge_captured       BOOLEAN,                       -- Did realized P&L direction match edge_type?
    closed_at           TIMESTAMPTZ,
    close_reason        trade_status                   -- How the trade was closed
);

CREATE INDEX IF NOT EXISTS idx_edge_system ON edge_ledger(system);
CREATE INDEX IF NOT EXISTS idx_edge_type ON edge_ledger(edge_type);
CREATE INDEX IF NOT EXISTS idx_edge_trade ON edge_ledger(trade_id);
CREATE INDEX IF NOT EXISTS idx_edge_ticker ON edge_ledger(ticker);


-- ============================================================================
-- TABLE: trades
-- Every executed trade (filled orders) for both systems.
-- One row per position lifecycle (open → close).
-- ============================================================================

CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL PRIMARY KEY,
    system              system_name NOT NULL,
    signal_id           BIGINT REFERENCES signals(id),
    edge_id             BIGINT,                        -- FK to edge_ledger.id

    -- Position identity
    ticker              VARCHAR(10) NOT NULL,
    structure           structure_type NOT NULL,
    
    -- Leg 1 (short leg for spreads, only leg for single options)
    leg1_symbol         VARCHAR(30) NOT NULL,          -- OCC option symbol
    leg1_type           option_type NOT NULL,
    leg1_strike         NUMERIC(12,4) NOT NULL,
    leg1_expiration     DATE NOT NULL,
    leg1_delta          NUMERIC(8,4),
    leg1_iv             NUMERIC(8,4),                  -- Per-leg IV (skew-aware)
    leg1_action         VARCHAR(4) NOT NULL,            -- 'SELL' or 'BUY'
    
    -- Leg 2 (long leg for spreads, NULL for single options)
    leg2_symbol         VARCHAR(30),
    leg2_type           option_type,
    leg2_strike         NUMERIC(12,4),
    leg2_expiration     DATE,
    leg2_delta          NUMERIC(8,4),
    leg2_iv             NUMERIC(8,4),
    leg2_action         VARCHAR(4),

    -- Entry
    entry_price         NUMERIC(12,4) NOT NULL,        -- Credit received (spreads) or premium paid (longs)
    entry_ts            TIMESTAMPTZ NOT NULL,
    entry_quantity      INTEGER NOT NULL DEFAULT 1,
    
    -- Greeks at entry (spread-level aggregate)
    entry_delta         NUMERIC(8,4),
    entry_gamma         NUMERIC(8,4),
    entry_theta         NUMERIC(8,4),
    entry_vega          NUMERIC(8,4),

    -- Risk parameters at entry
    max_profit          NUMERIC(12,4),                 -- Credit (spreads) or unlimited marker (longs)
    max_loss            NUMERIC(12,4),                 -- Spread width - credit (spreads) or premium (longs)
    kelly_fraction      NUMERIC(6,4),                  -- Kelly fraction used for sizing
    vix_at_entry        NUMERIC(8,4),                  -- VIX level at entry (for Kelly tier audit)
    capital_at_risk     NUMERIC(12,4),                 -- Actual $ at risk

    -- Exit (populated at close)
    exit_price          NUMERIC(12,4),
    exit_ts             TIMESTAMPTZ,
    exit_reason         trade_status,
    realized_pnl        NUMERIC(12,4),                 -- Net P&L after commissions
    commissions         NUMERIC(12,4),
    hold_duration_hours NUMERIC(12,2),                 -- Time in position

    -- Status
    status              trade_status NOT NULL DEFAULT 'PENDING',
    is_day_trade        BOOLEAN DEFAULT FALSE,         -- Intraday round-trip flag (observability only post-PDT; not a gate)
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_system ON trades(system);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts ON trades(entry_ts);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(status) 
    WHERE status IN ('PENDING', 'SUBMITTED', 'FILLED', 'PARTIAL_FILL');


-- ============================================================================
-- TABLE: is_checkpoints
-- Implementation Shortfall tracking per Velu et al. Chapter 10.
-- Three checkpoints per trade: decision, submission, execution.
-- ============================================================================

CREATE TABLE IF NOT EXISTS is_checkpoints (
    id                  BIGSERIAL PRIMARY KEY,
    trade_id            BIGINT REFERENCES trades(id),
    signal_id           BIGINT REFERENCES signals(id),
    system              system_name NOT NULL,

    -- Checkpoint 1: Decision (signal fire)
    decision_price      NUMERIC(12,4) NOT NULL,
    decision_ts         TIMESTAMPTZ NOT NULL,

    -- Checkpoint 2: Submission (order sent to IBKR)
    submit_price        NUMERIC(12,4),
    submit_ts           TIMESTAMPTZ,

    -- Checkpoint 3: Execution (fill confirmed)
    execution_price     NUMERIC(12,4),
    execution_ts        TIMESTAMPTZ,

    -- IS decomposition
    delay_cost          NUMERIC(12,6),                 -- decision → submit price change
    market_impact       NUMERIC(12,6),                 -- submit → execution price change
    opportunity_cost    NUMERIC(12,6),                 -- unfilled/cancelled cost (0 if filled)
    total_is            NUMERIC(12,6),                 -- Sum of all components
    is_as_pct_of_edge   NUMERIC(8,4),                  -- total_is / edge_magnitude (the critical ratio)

    -- Context
    ticker              VARCHAR(10) NOT NULL,
    spread_width        NUMERIC(8,4),                  -- Bid-ask spread at decision time
    underlying_volume   BIGINT,                        -- Underlying volume at decision time
    oi_at_strike        INTEGER,                       -- Open interest at the traded strike
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_is_trade ON is_checkpoints(trade_id);
CREATE INDEX IF NOT EXISTS idx_is_system ON is_checkpoints(system);
CREATE INDEX IF NOT EXISTS idx_is_ticker ON is_checkpoints(ticker);


-- ============================================================================
-- TABLE: intraday_trades
-- Post-PDT (FINRA PDT eliminated 2026-06-04): OBSERVABILITY ONLY, NOT A GATE.
-- One row per intraday round-trip (same-session open+close). Used for audit,
-- Kelly-tier attribution, and intraday-frequency analytics. There is NO slot
-- count and NO blocking semantics — the Kelly sizer + circuit breakers are the
-- sole frequency governors now. IBKR's real-time intraday margin engine is the
-- binding constraint that replaced PDT (wired in the risk engine, Phase 2+).
-- ============================================================================

CREATE TABLE IF NOT EXISTS intraday_trades (
    id                  BIGSERIAL PRIMARY KEY,
    system              system_name NOT NULL,
    trade_id            BIGINT REFERENCES trades(id),
    ticker              VARCHAR(10) NOT NULL,
    round_trip_ts       TIMESTAMPTZ NOT NULL,          -- Timestamp the round-trip completed (close)
    was_emergency       BOOLEAN NOT NULL DEFAULT FALSE, -- Emergency stop-loss exit?
    hold_duration_min   NUMERIC(10,2),                 -- Minutes held (open → close), for frequency analytics
    notes               TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Observability query (NOT a gate): count intraday round-trips in a window
--   SELECT COUNT(*) FROM intraday_trades
--   WHERE round_trip_ts > NOW() - INTERVAL '5 days';
-- This figure is surfaced on the dashboard for awareness only. It blocks nothing.

CREATE INDEX IF NOT EXISTS idx_intraday_ts ON intraday_trades(round_trip_ts);
CREATE INDEX IF NOT EXISTS idx_intraday_system ON intraday_trades(system);


-- ============================================================================
-- TABLE: capital_fence
-- Tracks capital allocation between systems.
-- One active row per system. Updated on every trade open/close and weekly rebalance.
-- ============================================================================

CREATE TABLE IF NOT EXISTS capital_fence (
    id                  BIGSERIAL PRIMARY KEY,
    system              system_name NOT NULL,
    
    -- Allocation
    base_allocation_pct NUMERIC(6,4) NOT NULL,         -- 0.70 for RAIJIN, 0.30 for SUSANOO
    current_allocation  NUMERIC(12,4) NOT NULL,        -- Current $ allocated to this system
    capital_in_use      NUMERIC(12,4) NOT NULL DEFAULT 0, -- Currently deployed (open position risk)
    capital_available   NUMERIC(12,4) NOT NULL,        -- available = allocation - in_use
    
    -- Account-level
    total_account_equity NUMERIC(12,4) NOT NULL,       -- Full IBKR account value
    
    -- Freeze state
    is_frozen           BOOLEAN NOT NULL DEFAULT FALSE, -- True if system is frozen by circuit breaker
    freeze_reason       TEXT,
    
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one active row per system
CREATE UNIQUE INDEX IF NOT EXISTS idx_fence_system ON capital_fence(system);


-- ============================================================================
-- TABLE: circuit_breaker_events
-- Log of every circuit breaker trigger for audit trail.
-- ============================================================================

CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id                  BIGSERIAL PRIMARY KEY,
    system              system_name,                   -- NULL = account-level breaker
    breaker_type        VARCHAR(30) NOT NULL,          -- 'DAILY_DD', 'PEAK_TROUGH', 'VIX_SPIKE', 'IS_BUDGET'
    trigger_value       NUMERIC(12,4),                 -- The value that triggered the breaker
    threshold_value     NUMERIC(12,4),                 -- The threshold that was breached
    action_taken        TEXT NOT NULL,                  -- 'PAUSE_NEW_ENTRIES', 'FULL_HALT', 'FREEZE_SUSANOO'
    resolved_at         TIMESTAMPTZ,                   -- NULL until manually cleared
    notes               TEXT,
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_breaker_type ON circuit_breaker_events(breaker_type);
CREATE INDEX IF NOT EXISTS idx_breaker_unresolved ON circuit_breaker_events(resolved_at) 
    WHERE resolved_at IS NULL;


-- ============================================================================
-- TABLE: daily_metrics
-- End-of-day snapshot for equity curve, drawdown tracking, and dashboard.
-- One row per trading day.
-- ============================================================================

CREATE TABLE IF NOT EXISTS daily_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    trading_date        DATE NOT NULL UNIQUE,
    
    -- Account-level
    account_equity      NUMERIC(12,4) NOT NULL,
    daily_pnl           NUMERIC(12,4),
    peak_equity         NUMERIC(12,4),                 -- Running peak for drawdown calc
    drawdown_pct        NUMERIC(8,4),                  -- Current drawdown from peak
    
    -- Per-system
    raijin_equity       NUMERIC(12,4),
    raijin_daily_pnl    NUMERIC(12,4),
    raijin_open_positions INTEGER DEFAULT 0,
    susanoo_equity      NUMERIC(12,4),
    susanoo_daily_pnl   NUMERIC(12,4),
    susanoo_open_positions INTEGER DEFAULT 0,
    
    -- Market context
    vix_close           NUMERIC(8,4),
    spy_close           NUMERIC(12,4),
    
    -- Infrastructure
    monthly_cost_accrued NUMERIC(12,4),                -- Running monthly infrastructure cost
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metrics_date ON daily_metrics(trading_date);


-- ============================================================================
-- SEED DATA
-- ============================================================================

-- Initialize capital fence with starting allocations
INSERT INTO capital_fence (system, base_allocation_pct, current_allocation, capital_available, total_account_equity)
VALUES 
    ('RAIJIN', 0.70, 3500.00, 3500.00, 5000.00),
    ('SUSANOO', 0.30, 1500.00, 1500.00, 5000.00)
ON CONFLICT (system) DO NOTHING;


-- ============================================================================
-- HELPER VIEWS
-- ============================================================================

-- View: Intraday round-trip summary (post-PDT — OBSERVABILITY ONLY).
-- Replaces the old v_pdt_slots_remaining slot math. Counts round-trips in the
-- trailing 5 days. This blocks nothing; it is surfaced for awareness.
CREATE OR REPLACE VIEW v_intraday_summary AS
SELECT
    COUNT(*) AS round_trips_5d,
    COUNT(*) FILTER (WHERE was_emergency) AS emergency_exits_5d,
    MAX(round_trip_ts) AS last_round_trip,
    AVG(hold_duration_min) AS avg_hold_min_5d
FROM intraday_trades
WHERE round_trip_ts > NOW() - INTERVAL '5 days';

-- Compatibility shim: the deployed dashboard may still query v_pdt_slots_remaining.
-- Post-PDT there are no slots, so this returns an "unlimited" shape (NULL slots_*),
-- exposing the real round-trip count under slots_used for awareness. Dashboard
-- should migrate to v_intraday_summary; this shim prevents a hard break meanwhile.
CREATE OR REPLACE VIEW v_pdt_slots_remaining AS
SELECT
    NULL::INTEGER AS slots_remaining,        -- NULL = no limit (PDT eliminated)
    NULL::INTEGER AS total_slots,
    (SELECT COUNT(*) FROM intraday_trades
       WHERE round_trip_ts > NOW() - INTERVAL '5 days') AS slots_used,
    (SELECT MAX(round_trip_ts) FROM intraday_trades) AS last_day_trade,
    NULL::TIMESTAMPTZ AS oldest_slot_frees_at;

-- View: Open positions across both systems
CREATE OR REPLACE VIEW v_open_positions AS
SELECT 
    t.id,
    t.system,
    t.ticker,
    t.structure,
    t.entry_price,
    t.entry_ts,
    t.capital_at_risk,
    t.max_loss,
    t.status,
    e.edge_type,
    e.edge_magnitude,
    t.leg1_expiration AS expiration,
    t.leg1_expiration - CURRENT_DATE AS dte_remaining
FROM trades t
LEFT JOIN edge_ledger e ON e.trade_id = t.id
WHERE t.status IN ('FILLED', 'PARTIAL_FILL');

-- View: Trailing IS ratio (last 20 trades per system)
CREATE OR REPLACE VIEW v_trailing_is_ratio AS
SELECT 
    isc.system,
    AVG(isc.is_as_pct_of_edge) AS avg_is_pct_of_edge,
    COUNT(*) AS trade_count,
    CASE 
        WHEN AVG(isc.is_as_pct_of_edge) > 0.25 THEN 'ALERT: IS exceeds 25% of edge'
        ELSE 'OK'
    END AS is_health
FROM is_checkpoints isc
WHERE isc.created_at > NOW() - INTERVAL '60 days'
GROUP BY isc.system;

-- View: Edge performance by type
CREATE OR REPLACE VIEW v_edge_performance AS
SELECT
    system,
    edge_type,
    COUNT(*) AS total_trades,
    COUNT(*) FILTER (WHERE edge_captured = TRUE) AS wins,
    ROUND(COUNT(*) FILTER (WHERE edge_captured = TRUE)::NUMERIC / NULLIF(COUNT(*), 0), 4) AS hit_rate,
    ROUND(AVG(realized_pnl), 2) AS avg_pnl,
    ROUND(SUM(realized_pnl), 2) AS total_pnl,
    ROUND(AVG(edge_magnitude), 2) AS avg_edge_predicted,
    ROUND(AVG(edge_confidence), 4) AS avg_confidence
FROM edge_ledger
WHERE closed_at IS NOT NULL
GROUP BY system, edge_type;
