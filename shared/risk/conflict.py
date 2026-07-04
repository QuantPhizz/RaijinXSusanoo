# =============================================================================
# TKO-AGENTS — Shared Risk: Cross-System Conflict Detector
# File: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/risk/conflict.py
# =============================================================================

import logging

import asyncpg

logger = logging.getLogger("tko-agents.conflict")


class ConflictDetector:
    """
    Prevents RAIJIN and SUSANOO from holding positions on the same underlying.

    Why this matters:
    RAIJIN sells premium (short vega) while SUSANOO buys premium (long vega).
    Simultaneous positions on the same underlying cancel Greek exposure —
    an expensive way to achieve net-zero. The conflict detector prevents this
    at the signal gate, before any capital is deployed.

    PDT NOTE: Slot arbitration is not a responsibility of this class.
    PDT rule eliminated June 4 2026. Conflict detection is purely about
    Greek exposure overlap between sibling systems.

    Post-PDT unlock: same-day spread rolls (RAIJIN Phase 1) mean a ticker
    could appear twice in the trades table on the same day — once for the
    original spread and once for the roll. The conflict check uses
    status IN ('FILLED', 'PARTIAL_FILL') to avoid false positives on
    CLOSED positions from earlier in the session.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def check_conflict(self, system: str, ticker: str) -> bool:
        """
        Returns True if the OTHER system has an open position on this ticker.
        Open = status IN ('FILLED', 'PARTIAL_FILL').
        """
        system = system.upper()
        sibling = "SUSANOO" if system == "RAIJIN" else "RAIJIN"

        row = await self.pool.fetchrow(
            """
            SELECT COUNT(*) AS n
            FROM trades
            WHERE ticker = $1
              AND system = $2
              AND status IN ('FILLED', 'PARTIAL_FILL')
            """,
            ticker.upper(), sibling,
        )
        count = int(row["n"]) if row else 0

        if count > 0:
            logger.info(
                f"[{system}] CONFLICT — {sibling} has {count} open "
                f"position(s) on {ticker}. Signal rejected."
            )
            return True
        return False

    async def get_conflicting_positions(self, ticker: str) -> list[dict]:
        """Returns details of any open positions on this ticker across both systems."""
        rows = await self.pool.fetch(
            """
            SELECT id, system, ticker, direction, status,
                   entry_ts, entry_price, contracts
            FROM trades
            WHERE ticker = $1
              AND status IN ('FILLED', 'PARTIAL_FILL')
            ORDER BY system, entry_ts DESC
            """,
            ticker.upper(),
        )
        return [dict(r) for r in rows]

    async def get_all_conflicts(self) -> list[dict]:
        """
        Scan all open positions for any ticker held by both systems simultaneously.
        Returns list of conflict records for monitoring dashboard.
        Should return empty in normal operation.
        """
        rows = await self.pool.fetch(
            """
            SELECT ticker, COUNT(DISTINCT system) AS system_count,
                   ARRAY_AGG(DISTINCT system) AS systems
            FROM trades
            WHERE status IN ('FILLED', 'PARTIAL_FILL')
            GROUP BY ticker
            HAVING COUNT(DISTINCT system) > 1
            """
        )
        conflicts = [dict(r) for r in rows]
        if conflicts:
            logger.warning(f"⚠️  Active conflicts detected: {conflicts}")
        return conflicts
