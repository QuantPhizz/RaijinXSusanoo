-- ============================================================================
-- MIGRATION 003 — Signal Provenance & Idempotency Defense
-- ============================================================================
-- Applies to:   tko-agents primary schema (Supabase, PostgreSQL 15+)
-- Depends on:   001_schema.sql (v1.1), 002_intraday_trades_purge.sql
-- Session:      Phase 0 Step 4 E2E bring-up — E2 closure + idempotency
-- Authored:     2026-07-04
-- Author:       Senior quant architect (E2E session, Option A)
--
-- ── WHAT THIS MIGRATION DOES ─────────────────────────────────────────────
-- Adds three columns to signals:
--   1. source           — data provider tag ('TRADINGVIEW_PINE', 'FLASHALPHA_FREE', ...)
--   2. fidelity         — data provider fidelity ('PRODUCTION' | 'PROXY' | 'FIXTURE' | 'UNKNOWN')
--   3. signal_dedup_key — deterministic sha256 hash for idempotent inserts
--
-- ── WHY ────────────────────────────────────────────────────────────────
-- E2 (per NEXT_SESSION_E2E_SPEC.md, Task E2):
--   "The row must record source and fidelity tags per the provider spec.
--    Failure mode: a PROXY signal written without its tag becomes
--    indistinguishable from a production signal later — corrupts Phase 5
--    filtering. The tag is the guard."
--
-- The SUSANOO fidelity guard in shared/config/risk_parameters.yaml reads:
--     susanoo.dormancy_locked_pending_fidelity: PRODUCTION
-- Prior to this migration, that field pointed at a column that did not exist.
-- The guard was documented, not enforced.
--
-- IDEMPOTENCY: signal_status enum already includes REJECTED_DUPLICATE, but
-- no constraint enforced it. TradingView retries webhooks on 5xx/timeout;
-- without a UNIQUE dedup key, a single Tunnel hiccup during E1 lands two
-- rows for one alert, producing a false "the path worked twice" reading.
--
-- ── FAIL-CLOSED DESIGN ─────────────────────────────────────────────────
-- • NOT NULL with 'UNKNOWN' defaults on source/fidelity: existing insert
--   paths don't break during rolling deployment; unpopulated fields are
--   loudly labeled as unknown rather than silently null.
-- • CHECK constraint on fidelity: typos fail at the DB layer, not silently.
-- • UNIQUE dedup key: duplicate INSERT fails loudly even if server.py's
--   own dedup check is bypassed — belt and suspenders.
-- • All changes wrapped in a single transaction: partial failure rolls back.
--
-- ── DOMINANT FAILURE MODE OF THIS MIGRATION ────────────────────────────
-- If applied while server.py is running and inserting rows via a schema
-- that predates this migration, the OLD server.py may INSERT rows without
-- source/fidelity/dedup_key values. That's safe under this migration
-- because the columns default to 'UNKNOWN' / are backfilled — but the
-- OLD server won't populate dedup_key correctly, and TWO OLD-server
-- inserts for the same alert would land as two rows (dedup requires
-- server-side computation of the key). Mitigation: restart server.py
-- AFTER migration commits AND AFTER the corresponding server.py patch
-- ships. Do NOT deploy this migration and leave the old server running.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- STEP 1 — Add columns as nullable (safe: existing rows unaffected)
-- ---------------------------------------------------------------------------

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS source TEXT,
    ADD COLUMN IF NOT EXISTS fidelity TEXT,
    ADD COLUMN IF NOT EXISTS signal_dedup_key TEXT;


-- ---------------------------------------------------------------------------
-- STEP 2 — Backfill existing rows with conservative defaults
--
-- Existing rows (synthetic curls from session 2026-07-03) have raw_payload
-- populated but no provenance tags. Retroactive assignment:
--   source   = 'SYNTHETIC_CURL' (they came from curl, not Pine or a provider)
--   fidelity = 'UNKNOWN'        (no gate evaluation ran on them)
--   dedup_key = sha256 of canonical fields (computed below)
--
-- Backfill formula matches the Python computation in server.py exactly:
--     sha256(system || '|' || ticker || '|' || strategy || '|' || direction || '|' || timestamp)
-- Delimiter '|' prevents field-boundary collisions (e.g. system='RAIJIN' +
-- ticker='SPYBUY' vs system='RAIJINSPY' + ticker='BUY').
-- COALESCE guards against missing 'strategy' field in older syntheticals.
-- ---------------------------------------------------------------------------

UPDATE signals
SET source = 'SYNTHETIC_CURL'
WHERE source IS NULL;

UPDATE signals
SET fidelity = 'UNKNOWN'
WHERE fidelity IS NULL;

UPDATE signals
SET signal_dedup_key = encode(
    sha256(
        (
            system::text || '|' ||
            ticker || '|' ||
            COALESCE(raw_payload->>'strategy', '') || '|' ||
            direction || '|' ||
            COALESCE(raw_payload->>'timestamp', received_at::text)
        )::bytea
    ),
    'hex'
)
WHERE signal_dedup_key IS NULL;


-- ---------------------------------------------------------------------------
-- STEP 3 — Enforce NOT NULL + defaults for future inserts
--
-- Defaults ensure that even if server.py is momentarily rolled back to a
-- version that doesn't populate these fields, inserts don't fail — they
-- land as 'UNKNOWN', which is loud enough to catch in review.
-- ---------------------------------------------------------------------------

ALTER TABLE signals
    ALTER COLUMN source SET NOT NULL,
    ALTER COLUMN source SET DEFAULT 'UNKNOWN';

ALTER TABLE signals
    ALTER COLUMN fidelity SET NOT NULL,
    ALTER COLUMN fidelity SET DEFAULT 'UNKNOWN';

ALTER TABLE signals
    ALTER COLUMN signal_dedup_key SET NOT NULL;
    -- Intentionally NO default on signal_dedup_key: it MUST be computed by
    -- the server at ingress. A default here would let un-keyed rows slip in.


-- ---------------------------------------------------------------------------
-- STEP 4 — Fidelity CHECK constraint (enum-like)
--
-- Chose TEXT + CHECK over Postgres ENUM for two reasons:
--   1. Adding values to a Postgres ENUM requires ALTER TYPE ADD VALUE,
--      which cannot run inside a transaction — future migrations awkward.
--   2. CHECK constraint is easier to audit, drop, and re-add cleanly.
-- ---------------------------------------------------------------------------

ALTER TABLE signals
    ADD CONSTRAINT signals_fidelity_check
    CHECK (fidelity IN ('PRODUCTION', 'PROXY', 'FIXTURE', 'UNKNOWN'));

-- source is intentionally left free-form for now. As the provider set
-- stabilizes (post-Phase 1), tighten this to a CHECK constraint too.


-- ---------------------------------------------------------------------------
-- STEP 5 — UNIQUE constraint on dedup_key (the actual retry defense)
--
-- server.py MUST use ON CONFLICT (signal_dedup_key) DO NOTHING or catch
-- asyncpg.UniqueViolationError and return the existing signal_id.
-- See SERVER_PATCH_003.md for the exact insert pattern.
-- ---------------------------------------------------------------------------

ALTER TABLE signals
    ADD CONSTRAINT signals_dedup_key_unique UNIQUE (signal_dedup_key);


-- ---------------------------------------------------------------------------
-- STEP 6 — Index fidelity for Phase 5 attribution queries
--
-- Separate from the PK index because Phase 5 will scan by fidelity to
-- filter PROXY signals out of live-performance analysis. Without this
-- index, that scan is a full table read.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS signals_fidelity_idx ON signals (fidelity);


COMMIT;


-- ============================================================================
-- POST-MIGRATION VERIFICATION (run manually after COMMIT)
-- ============================================================================
-- All four queries below must return the expected result before the
-- migration is considered green.

-- 1. Existing rows have all three new fields populated (expect NON-NULL for all)
--    SELECT id, source, fidelity, signal_dedup_key FROM signals ORDER BY id;

-- 2. Both new constraints exist
--    SELECT conname, contype FROM pg_constraint
--    WHERE conrelid = 'signals'::regclass
--      AND conname IN ('signals_fidelity_check', 'signals_dedup_key_unique');
--    (Expect 2 rows: one 'c' (check), one 'u' (unique))

-- 3. No duplicate dedup keys (expect 0 rows)
--    SELECT signal_dedup_key, COUNT(*) FROM signals
--    GROUP BY signal_dedup_key HAVING COUNT(*) > 1;

-- 4. All fidelity values pass the check constraint (expect only allowed enum values)
--    SELECT fidelity, COUNT(*) FROM signals GROUP BY fidelity ORDER BY fidelity;


-- ============================================================================
-- ROLLBACK — DESTRUCTIVE, use only if migration is being reverted entirely
-- ============================================================================
-- Do NOT drop columns without first verifying no downstream consumer
-- references them (grep for 'source', 'fidelity', 'signal_dedup_key' in
-- server.py, sizing.py, dashboard/, and analytics/).
--
-- BEGIN;
-- ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_dedup_key_unique;
-- ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_fidelity_check;
-- DROP INDEX IF EXISTS signals_fidelity_idx;
-- ALTER TABLE signals DROP COLUMN IF EXISTS signal_dedup_key;
-- ALTER TABLE signals DROP COLUMN IF EXISTS fidelity;
-- ALTER TABLE signals DROP COLUMN IF EXISTS source;
-- COMMIT;
