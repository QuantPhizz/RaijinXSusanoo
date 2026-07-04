# =============================================================================
# TKO-AGENTS — Shared Risk: Circuit Breakers
# File: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/risk/circuit_breakers.py
# =============================================================================

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("tko-agents.circuit_breakers")

# Breaker thresholds — sourced from Cardinal Rules
DAILY_DD_THRESHOLD    = -0.02   # -2% daily P&L → pause new entries
PEAK_TROUGH_THRESHOLD = -0.08   # -8% from peak → full halt
VIX_SPIKE_TRIGGER     = 30.0    # VIX > 30 → RAIJIN long-only mode
VIX_SPIKE_RESOLVE     = 28.0    # VIX < 28 → auto-resolve
IS_BUDGET_THRESHOLD   = 0.25    # IS > 25% of avg edge → pause system


class CircuitBreakerManager:
    """
    Account-level and system-level circuit breakers.
    All events log to circuit_breaker_events table.

    Breaker matrix:
    ┌──────────────┬─────────┬─────────────────────┬─────────────────────────┬──────────────┐
    │ Breaker      │ Scope   │ Threshold           │ Action                  │ Auto-resolve │
    ├──────────────┼─────────┼─────────────────────┼─────────────────────────┼──────────────┤
    │ DAILY_DD     │ Account │ -2% daily P&L       │ Pause new entries       │ Yes — next open │
    │ PEAK_TROUGH  │ Account │ -8% from peak       │ Full halt               │ No — manual  │
    │ VIX_SPIKE    │ Market  │ VIX > 30            │ RAIJIN long-only        │ Yes — VIX<28 │
    │ IS_BUDGET    │ System  │ IS > 25% of edge    │ Pause that system       │ No — investigate │
    │ EARNINGS_TRAP│ System  │ Position in ex-zone │ Alert + recompute       │ No — manual  │
    └──────────────┴─────────┴─────────────────────┴─────────────────────────┴──────────────┘

    PDT NOTE: No PDT-related breaker exists. PDT rule eliminated June 4 2026.
    Intraday frequency is no longer a circuit breaker input.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def check_all(self, system: str) -> list[str]:
        """
        Run all breaker checks for the given system.
        Returns list of triggered breaker names (empty = all clear).
        Called before every trade entry.
        """
        system = system.upper()
        triggered = []

        if await self.check_daily_drawdown():
            triggered.append("DAILY_DD")

        if await self.check_peak_to_trough():
            triggered.append("PEAK_TROUGH")

        if await self.check_vix_spike():
            triggered.append("VIX_SPIKE")

        if await self.check_is_budget(system):
            triggered.append("IS_BUDGET")

        if triggered:
            logger.warning(f"[{system}] breakers triggered: {triggered}")

        return triggered

    async def check_daily_drawdown(self) -> bool:
        """
        Account-level: today's P&L worse than -2% (-$100 on $5k)?
        If triggered: pause new entries for both systems.
        Auto-resolves at next market open.
        Phase 0: always returns False (no real P&L yet).
        """
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) AS daily_pnl
            FROM trades
            WHERE DATE(exit_ts) = CURRENT_DATE
              AND status = 'CLOSED'
            """
        )
        daily_pnl = float(row["daily_pnl"]) if row else 0.0

        # Get total account equity for % calculation
        equity_row = await self.pool.fetchrow(
            "SELECT SUM(current_allocation) AS total FROM capital_fence"
        )
        total_equity = float(equity_row["total"]) if equity_row else 5000.0

        pct = daily_pnl / total_equity if total_equity > 0 else 0.0

        if pct <= DAILY_DD_THRESHOLD:
            logger.warning(
                f"DAILY_DD breaker: daily P&L {pct:.1%} <= {DAILY_DD_THRESHOLD:.0%}"
            )
            await self._log_breaker(
                system=None,
                breaker_type="DAILY_DD",
                trigger_value=pct,
                threshold=DAILY_DD_THRESHOLD,
                action="PAUSE_NEW_ENTRIES — both systems",
            )
            return True
        return False

    async def check_peak_to_trough(self) -> bool:
        """
        Account-level: current equity more than 8% below peak?
        If triggered: full halt, manual review required.
        Phase 0: always returns False (no real equity curve yet).
        """
        row = await self.pool.fetchrow(
            """
            SELECT MAX(closing_equity) AS peak, MIN(closing_equity) AS trough
            FROM daily_metrics
            WHERE trading_date >= CURRENT_DATE - INTERVAL '90 days'
            """
        )
        if not row or not row["peak"] or not row["trough"]:
            return False

        peak   = float(row["peak"])
        trough = float(row["trough"])
        dd     = (trough - peak) / peak if peak > 0 else 0.0

        if dd <= PEAK_TROUGH_THRESHOLD:
            logger.warning(
                f"PEAK_TROUGH breaker: drawdown {dd:.1%} <= {PEAK_TROUGH_THRESHOLD:.0%}"
            )
            await self._log_breaker(
                system=None,
                breaker_type="PEAK_TROUGH",
                trigger_value=dd,
                threshold=PEAK_TROUGH_THRESHOLD,
                action="FULL_HALT — manual review required",
            )
            return True
        return False

    async def check_vix_spike(self) -> bool:
        """
        Market-level: VIX > 30?
        RAIJIN switches to long-premium-only (no new credit spreads).
        SUSANOO is unaffected — it only buys premium.
        Auto-resolves when VIX drops below 28.
        Phase 0: reads VIX from last received signal payload.
        Phase 1+: reads VIX from Polygon/market data feed.
        """
        row = await self.pool.fetchrow(
            """
            SELECT (raw_payload->>'vix')::float AS vix
            FROM signals
            WHERE raw_payload->>'vix' IS NOT NULL
            ORDER BY received_ts DESC
            LIMIT 1
            """
        )
        if not row or not row["vix"]:
            return False  # No VIX data yet — don't block

        vix = float(row["vix"])

        # Check if already triggered and VIX has recovered
        open_event = await self.pool.fetchrow(
            """
            SELECT id FROM circuit_breaker_events
            WHERE breaker_type = 'VIX_SPIKE' AND resolved_at IS NULL
            LIMIT 1
            """
        )
        if open_event and vix < VIX_SPIKE_RESOLVE:
            await self.resolve_breaker(
                open_event["id"],
                f"VIX recovered to {vix:.1f} (below {VIX_SPIKE_RESOLVE})",
            )
            return False

        if vix > VIX_SPIKE_TRIGGER and not open_event:
            logger.warning(f"VIX_SPIKE breaker: VIX={vix:.1f} > {VIX_SPIKE_TRIGGER}")
            await self._log_breaker(
                system="RAIJIN",
                breaker_type="VIX_SPIKE",
                trigger_value=vix,
                threshold=VIX_SPIKE_TRIGGER,
                action="RAIJIN_LONG_ONLY — no new credit spreads. SUSANOO unaffected.",
            )
            return True

        return bool(open_event and vix >= VIX_SPIKE_RESOLVE)

    async def check_is_budget(self, system: str) -> bool:
        """
        System-level: trailing 20-trade IS > 25% of avg edge_magnitude?
        If triggered: pause new entries for that system until IS improves.
        IS = Implementation Shortfall (slippage vs decision price).
        Phase 0: always returns False (no fills yet).
        """
        row = await self.pool.fetchrow(
            """
            SELECT * FROM v_trailing_is_ratio WHERE system = $1
            """,
            system,
        )
        if not row:
            return False

        is_ratio = float(row.get("is_ratio", 0) or 0)

        if is_ratio > IS_BUDGET_THRESHOLD:
            logger.warning(
                f"[{system}] IS_BUDGET breaker: IS ratio {is_ratio:.1%} > {IS_BUDGET_THRESHOLD:.0%}"
            )
            await self._log_breaker(
                system=system,
                breaker_type="IS_BUDGET",
                trigger_value=is_ratio,
                threshold=IS_BUDGET_THRESHOLD,
                action=f"PAUSE_{system} — investigate execution quality",
            )
            return True
        return False

    async def resolve_breaker(self, breaker_id: int, notes: str):
        """Manual or auto resolution of a circuit breaker event."""
        await self.pool.execute(
            """
            UPDATE circuit_breaker_events
            SET resolved_at = NOW(), notes = $1
            WHERE id = $2
            """,
            notes, breaker_id,
        )
        logger.info(f"Breaker #{breaker_id} resolved: {notes}")

    async def get_active_breakers(self, system: str | None = None) -> list[dict]:
        """Returns all unresolved circuit breaker events."""
        if system:
            rows = await self.pool.fetch(
                """
                SELECT * FROM circuit_breaker_events
                WHERE resolved_at IS NULL
                  AND (system = $1 OR system IS NULL)
                ORDER BY triggered_at DESC
                """,
                system,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT * FROM circuit_breaker_events
                WHERE resolved_at IS NULL
                ORDER BY triggered_at DESC
                """
            )
        return [dict(r) for r in rows]

    async def _log_breaker(
        self,
        system: str | None,
        breaker_type: str,
        trigger_value: float | None,
        threshold: float | None,
        action: str,
    ):
        """Insert a circuit breaker event. Deduplicates open events of same type+system."""
        existing = await self.pool.fetchrow(
            """
            SELECT id FROM circuit_breaker_events
            WHERE breaker_type = $1
              AND (system = $2 OR (system IS NULL AND $2 IS NULL))
              AND resolved_at IS NULL
            """,
            breaker_type, system,
        )
        if existing:
            return  # Already logged — don't duplicate

        await self.pool.execute(
            """
            INSERT INTO circuit_breaker_events
                (system, breaker_type, trigger_value, threshold_value, action_taken)
            VALUES ($1, $2, $3, $4, $5)
            """,
            system, breaker_type, trigger_value, threshold, action,
        )
