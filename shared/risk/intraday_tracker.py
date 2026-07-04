# =============================================================================
# TKO-AGENTS — Shared Risk: Intraday Tracker
# File: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/risk/intraday_tracker.py
#
# Replaces: pdt.py (PDT rule eliminated June 4 2026, FINRA Rule 4210)
#
# Purpose: Track same-day open+close trades for intraday margin monitoring.
# The binding constraint is now IBKR's real-time intraday margin engine —
# NOT a day-trade count. This module exists purely for observability and
# to feed the IS framework with intraday execution quality data.
#
# What was REMOVED vs pdt.py spec:
#   - Slot allocation table (3-slot budget)
#   - SUSANOO slot block (slots_remaining == 1 → SUSANOO False)
#   - can_day_trade() arbitration logic
#   - get_slots_remaining() view query
#   - PDTExhaustedError
#
# What was ADDED:
#   - intraday flag forwarded to sizing for margin headroom awareness (Phase 1+)
#   - Same-day roll detection for RAIJIN adjustment logic (Phase 1+)
# =============================================================================

import logging
from datetime import datetime, timezone

import asyncpg
import exchange_calendars as xcals

logger = logging.getLogger("tko-agents.intraday_tracker")

NYSE = xcals.get_calendar("XNYS")


# ---------------------------------------------------------------------------
# Trading day utilities
# ---------------------------------------------------------------------------

def is_same_trading_day(ts1: datetime, ts2: datetime) -> bool:
    """
    Returns True if both timestamps fall on the same NYSE trading session.
    Uses exchange_calendars — handles half-days, holidays correctly.
    Correct as of June 4 2026 and forward.
    """
    try:
        # exchange_calendars expects timezone-aware datetimes
        if ts1.tzinfo is None:
            ts1 = ts1.replace(tzinfo=timezone.utc)
        if ts2.tzinfo is None:
            ts2 = ts2.replace(tzinfo=timezone.utc)
        session1 = NYSE.minute_to_session(ts1)
        session2 = NYSE.minute_to_session(ts2)
        return session1 == session2
    except Exception as e:
        # Outside market hours — conservatively return False (not same session)
        logger.warning(f"is_same_trading_day fallback: {e}")
        return ts1.date() == ts2.date()


def is_market_hours(ts: datetime | None = None) -> bool:
    """Returns True if ts (or now) falls within NYSE regular session."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        return NYSE.is_open_on_minute(ts)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# IntradayTracker
# ---------------------------------------------------------------------------

class IntradayTracker:
    """
    Tracks same-day open+close trades for intraday margin monitoring.

    PDT CONTEXT (June 4 2026):
    The PDT rule (FINRA Rule 4210) has been eliminated. There is no 3-slot
    budget, no day-trade count limit, and no slot arbitration between RAIJIN
    and SUSANOO. This class is NOT a gate — it is an observability tool.

    The binding constraint on intraday frequency is IBKR's real-time
    intraday margin engine, which updates continuously based on actual
    position exposure. Phase 1+ will wire IBKR buying power into the
    risk engine for pre-trade margin headroom checks.

    Strategy unlocks enabled by PDT elimination:
      RAIJIN:
        - Same-day close on stop-loss: always immediate, no slot cost
        - Same-day spread rolls: valid adjustment tactic (Phase 1+)
      SUSANOO:
        - Intraday debit spreads on high-conviction catalyst setups
          when intraday=True gate is set (Phase S1+)
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def record_intraday_trade(
        self,
        system: str,
        trade_id: int,
        ticker: str,
        action: str,              # 'OPEN', 'CLOSE', 'ROLL'
        is_same_day_close: bool,
        was_roll: bool = False,
    ) -> int:
        """
        Record an intraday trade event in intraday_trades table.
        Returns inserted row id.
        No slot check — no slot limit exists.
        """
        row = await self.pool.fetchrow(
            """
            INSERT INTO intraday_trades (
                system, trade_id, ticker, action,
                is_same_day_close, was_roll, opened_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            RETURNING id
            """,
            system, trade_id, ticker, action,
            is_same_day_close, was_roll,
        )
        logger.info(
            f"[{system}] intraday record #{row['id']} | "
            f"{ticker} {action} | same_day_close={is_same_day_close} "
            f"roll={was_roll}"
        )
        return row["id"]

    async def would_be_same_day_close(self, trade_id: int) -> bool:
        """
        Returns True if closing trade_id now would be a same-day close.
        Used for IS checkpoint tagging and intraday margin awareness.
        NOT a gate — same-day closes are always permitted post-June 4 2026.
        """
        row = await self.pool.fetchrow(
            "SELECT entry_ts FROM trades WHERE id = $1", trade_id
        )
        if not row:
            return False
        return is_same_trading_day(row["entry_ts"], datetime.now(timezone.utc))

    async def get_today_summary(self) -> dict:
        """
        Returns intraday trade counts for the current session.
        No slot budget — counts are for observability only.
        """
        row = await self.pool.fetchrow("SELECT * FROM v_intraday_summary")
        if row:
            return dict(row)
        return {
            "intraday_trades_today": 0,
            "raijin_intraday": 0,
            "susanoo_intraday": 0,
            "trading_date": datetime.now(timezone.utc).date().isoformat(),
        }

    async def get_oldest_intraday_open(self, system: str) -> datetime | None:
        """
        Returns the entry_ts of the oldest open intraday position for system.
        Used by sizing module for margin exposure estimation (Phase 1+).
        """
        row = await self.pool.fetchrow(
            """
            SELECT MIN(opened_at) AS oldest
            FROM intraday_trades
            WHERE system = $1
              AND DATE(opened_at) = CURRENT_DATE
              AND action = 'OPEN'
            """,
            system,
        )
        return row["oldest"] if row else None
