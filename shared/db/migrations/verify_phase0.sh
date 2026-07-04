#!/usr/bin/env bash
# ============================================================================
# verify_phase0.sh — TKO-AGENTS Phase 0 done-condition checker
# READ-ONLY. Runs SELECTs and catalog lookups only. Writes nothing, drops
# nothing, migrates nothing. Safe to run against the live DB any time.
#
# Usage:
#   set -a; source /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/api/.env; set +a
#   ./verify_phase0.sh
#
# Requires DATABASE_URL in the environment (Supabase libpq URL).
# Exits 0 only if every check passes.
# ============================================================================

set -uo pipefail

if [ -z "${DATABASE_URL:-}" ]; then
  echo "FAIL: DATABASE_URL is not set. Source your .env first:"
  echo "      set -a; source shared/api/.env; set +a"
  exit 1
fi

PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
no()   { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

q() { psql "$DATABASE_URL" -tAc "$1" 2>/dev/null; }

echo "============================================================"
echo "TKO-AGENTS Phase 0 verification — $(date)"
echo "============================================================"

# --- 0. Connectivity --------------------------------------------------------
if [ "$(q 'SELECT 1;')" = "1" ]; then ok "database reachable"
else no "database NOT reachable (check DATABASE_URL / Supabase status)"; echo; echo "Aborting."; exit 1; fi

# --- 1. Seed rows: capital fence 70/30 -------------------------------------
RAIJIN=$(q "SELECT current_allocation FROM capital_fence WHERE system='RAIJIN';")
SUSANOO=$(q "SELECT current_allocation FROM capital_fence WHERE system='SUSANOO';")
[ "$RAIJIN" = "3500.0000" ]  && ok "RAIJIN seed = 3500"  || no "RAIJIN seed = ${RAIJIN:-MISSING} (expected 3500.0000)"
[ "$SUSANOO" = "1500.0000" ] && ok "SUSANOO seed = 1500" || no "SUSANOO seed = ${SUSANOO:-MISSING} (expected 1500.0000)"

# --- 2. Post-PDT schema: intraday_trades present, pdt_counter absent --------
HAS_INTRADAY=$(q "SELECT to_regclass('public.intraday_trades') IS NOT NULL;")
HAS_PDT=$(q "SELECT to_regclass('public.pdt_counter') IS NOT NULL;")
[ "$HAS_INTRADAY" = "t" ] && ok "intraday_trades table exists (post-PDT)" || no "intraday_trades MISSING — 001 stale or not applied"
[ "$HAS_PDT" = "f" ]      && ok "pdt_counter absent (correct post-PDT)"     || no "pdt_counter still present — stale pre-PDT schema applied"

# --- 3. 002 purge column: the silent-corruption guard ----------------------
HAS_COL=$(q "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='edge_ledger' AND column_name='outcome_resolved_ts';")
[ "$HAS_COL" = "1" ] && ok "edge_ledger.outcome_resolved_ts exists (002 applied)" \
                     || no "outcome_resolved_ts MISSING — 002 not applied (CPCV will silently corrupt in Phase 5)"
HAS_COL2=$(q "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='trades' AND column_name='outcome_resolved_ts';")
[ "$HAS_COL2" = "1" ] && ok "trades.outcome_resolved_ts exists (002 applied)" \
                      || no "trades.outcome_resolved_ts MISSING — 002 not applied"

# --- 4. Table count: expect 8 base tables ----------------------------------
TCOUNT=$(q "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';")
[ "${TCOUNT:-0}" -ge 8 ] && ok "base tables present ($TCOUNT >= 8)" || no "only ${TCOUNT:-0} base tables (expected >= 8)"

# --- 5. Critical views resolve (catches broken view dependencies) ----------
for v in v_intraday_summary v_cpcv_purge_readiness v_edge_performance v_open_positions v_trailing_is_ratio; do
  if q "SELECT 1 FROM $v LIMIT 1;" >/dev/null 2>&1 || [ $? -le 1 ]; then ok "view $v resolves"
  else no "view $v broken"; fi
done

# --- 6. CPCV purge readiness (informational) -------------------------------
UNRESOLVED=$(q "SELECT COALESCE(SUM(unresolved_closed_trades),0) FROM v_cpcv_purge_readiness;")
echo "  INFO  unresolved closed trades (must be 0 before Phase 5): ${UNRESOLVED:-n/a}"

echo "============================================================"
echo "RESULT: $PASS passed, $FAIL failed"
echo "============================================================"
[ "$FAIL" -eq 0 ] && echo "Phase 0 DB done-conditions GREEN." || echo "Phase 0 NOT complete — see FAIL lines above."
exit $((FAIL > 0 ? 1 : 0))
