-- ============================================================================
-- TKO-AGENTS MIGRATION 002 — CPCV PURGE COLUMN
-- Apply AFTER 001_schema.sql. Idempotent (IF NOT EXISTS on all objects).
--
-- WHY THIS EXISTS (read before skipping):
--   Phase 5 validation uses Combinatorial Purged Cross-Validation (CPCV, after
--   López de Prado; Velu et al. Ch. on backtest overfitting). CPCV must PURGE
--   training observations whose outcome window overlaps the test window, or the
--   PBO (Probability of Backtest Overfitting) is computed on leaked data and
--   comes back optimistically biased — i.e. a strategy that looks like it clears
--   PBO < 0.40 when it does not. The purge needs the timestamp at which each
--   trade's OUTCOME was actually known (not entry, not the row's updated_at).
--
--   The dominant failure mode this guards against: applying 001 but forgetting
--   002. The column goes silently missing, CPCV falls back to un-purged folds,
--   and Phase 5 greenlights an overfit strategy. Apply both. Verify both.
--
-- This migration is the schema-of-record for the purge column. Do not add the
-- column directly to a live DB without committing it here first.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. edge_ledger.outcome_resolved_ts
--    The moment the trade's edge outcome became known (position fully closed
--    AND realized_pnl + edge_captured populated). This is the CPCV label-end
--    timestamp. Distinct from closed_at: closed_at is when the position closed;
--    outcome_resolved_ts is when the labeled outcome was committed for ML use.
--    In practice they are usually equal, but keeping them separate lets late
--    attribution corrections (e.g. commission settlement) move the resolution
--    time without rewriting close history.
-- ----------------------------------------------------------------------------

ALTER TABLE edge_ledger
    ADD COLUMN IF NOT EXISTS outcome_resolved_ts TIMESTAMPTZ;

COMMENT ON COLUMN edge_ledger.outcome_resolved_ts IS
    'CPCV purge boundary: timestamp the labeled outcome was committed. '
    'Used to purge overlapping train/test observations in Phase 5 validation. '
    'NULL until the trade outcome is resolved.';

-- Backfill for any rows already closed before this migration: best-available
-- proxy is closed_at. Only touches resolved rows; leaves open trades NULL.
UPDATE edge_ledger
    SET outcome_resolved_ts = closed_at
    WHERE outcome_resolved_ts IS NULL
      AND closed_at IS NOT NULL;

-- Index: CPCV scans this column heavily when building purged folds.
CREATE INDEX IF NOT EXISTS idx_edge_outcome_resolved
    ON edge_ledger(outcome_resolved_ts);

-- ----------------------------------------------------------------------------
-- 2. trades.outcome_resolved_ts
--    Mirror on trades for queries that join from the position side. Same
--    semantics. Kept in sync by the execution/attribution layer at close.
-- ----------------------------------------------------------------------------

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS outcome_resolved_ts TIMESTAMPTZ;

COMMENT ON COLUMN trades.outcome_resolved_ts IS
    'CPCV purge boundary (position side). Mirrors edge_ledger.outcome_resolved_ts.';

UPDATE trades
    SET outcome_resolved_ts = exit_ts
    WHERE outcome_resolved_ts IS NULL
      AND exit_ts IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trades_outcome_resolved
    ON trades(outcome_resolved_ts);

-- ----------------------------------------------------------------------------
-- 3. Validation view: surfaces purge-readiness so Phase 5 can assert before run.
--    If unresolved_closed_trades > 0, some closed trades lack a resolution ts —
--    CPCV would silently drop or mis-window them. Phase 5 should refuse to run
--    while this is non-zero.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_cpcv_purge_readiness AS
SELECT
    system,
    COUNT(*) FILTER (WHERE closed_at IS NOT NULL) AS closed_trades,
    COUNT(*) FILTER (WHERE closed_at IS NOT NULL
                       AND outcome_resolved_ts IS NULL) AS unresolved_closed_trades,
    MIN(outcome_resolved_ts) AS earliest_resolution,
    MAX(outcome_resolved_ts) AS latest_resolution
FROM edge_ledger
GROUP BY system;

-- ============================================================================
-- DONE. Verify with 003_verify (or verify_phase0.sh):
--   SELECT column_name FROM information_schema.columns
--     WHERE table_name='edge_ledger' AND column_name='outcome_resolved_ts';
--   -> must return exactly one row.
-- ============================================================================
