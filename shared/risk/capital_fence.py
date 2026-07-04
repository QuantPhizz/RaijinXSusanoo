# =============================================================================
# TKO-AGENTS — Shared Risk: Capital Fence
# File: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/risk/capital_fence.py
# =============================================================================

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("tko-agents.capital_fence")

# Drawdown thresholds → freeze action
# SUSANOO always freezes first (secondary system)
FREEZE_THRESHOLDS = [
    (0.08, ["SUSANOO", "RAIJIN"]),  # 8% combined DD → full halt
    (0.06, ["SUSANOO"]),             # 6% combined DD → freeze SUSANOO
    (0.04, []),                      # 4% combined DD → alert only
]


class CapitalFence:
    """
    Enforces the 70/30 capital split between RAIJIN and SUSANOO.
    Reads and writes the capital_fence table.

    Allocation: RAIJIN $3,500 (70%) | SUSANOO $1,500 (30%)
    Total account: $5,000

    PDT NOTE: Slot arbitration is NOT a responsibility of this class.
    The PDT rule was eliminated June 4 2026. Capital availability is
    the binding per-system constraint, enforced here.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_allocation(self, system: str) -> dict:
        """
        Returns current allocation for the system:
        {
            "system": "RAIJIN",
            "allocation": 3500.00,
            "capital_in_use": 750.00,
            "capital_available": 2750.00,
            "is_frozen": False,
            "freeze_reason": None
        }
        """
        system = system.upper()
        row = await self.pool.fetchrow(
            """
            SELECT system, current_allocation,
                   capital_in_use, capital_available,
                   is_frozen, freeze_reason
            FROM capital_fence
            WHERE system = $1
            """,
            system,
        )
        if not row:
            raise ValueError(f"No capital_fence record for system: {system}")
        return dict(row)

    async def can_allocate(self, system: str, amount: float) -> bool:
        """
        Returns True if the system can deploy $amount.
        Rules:
          1. System is not frozen
          2. amount <= capital_available for the system
          3. Combined unrealized drawdown < 6% (SUSANOO freeze threshold)
        """
        alloc = await self.get_allocation(system)

        if alloc["is_frozen"]:
            logger.warning(f"[{system}] can_allocate=False — system is frozen: {alloc['freeze_reason']}")
            return False

        if amount > float(alloc["capital_available"]):
            logger.info(
                f"[{system}] can_allocate=False — "
                f"requested ${amount:.2f} > available ${float(alloc['capital_available']):.2f}"
            )
            return False

        # Check combined drawdown
        dd = await self._combined_drawdown_pct()
        if dd >= 0.06 and system == "SUSANOO":
            logger.warning(
                f"[SUSANOO] can_allocate=False — "
                f"combined drawdown {dd:.1%} >= 6% SUSANOO freeze threshold"
            )
            return False
        if dd >= 0.08:
            logger.warning(
                f"[{system}] can_allocate=False — "
                f"combined drawdown {dd:.1%} >= 8% full halt threshold"
            )
            return False

        return True

    async def deploy_capital(self, system: str, trade_id: int, amount: float):
        """
        Mark $amount as deployed for a filled trade.
        Updates capital_in_use and capital_available.
        Called at trade entry after fill confirmation.
        """
        system = system.upper()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE capital_fence
                SET capital_in_use    = capital_in_use + $1,
                    capital_available = capital_available - $1,
                    updated_at        = NOW()
                WHERE system = $2
                """,
                amount, system,
            )
        logger.info(f"[{system}] deployed ${amount:.2f} for trade #{trade_id}")

    async def release_capital(
        self, system: str, trade_id: int, amount: float, pnl: float
    ):
        """
        Release $amount back to available pool, adjusted by P&L.
        Called at trade exit.
        Net return = amount + pnl (pnl is negative for losses).
        """
        system = system.upper()
        net_return = amount + pnl
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE capital_fence
                SET capital_in_use      = GREATEST(0, capital_in_use - $1),
                    capital_available   = capital_available + $2,
                    current_allocation  = current_allocation + $3,
                    updated_at          = NOW()
                WHERE system = $4
                """,
                amount,           # reduce in_use by original amount
                net_return,       # return net amount to available
                pnl,              # adjust total allocation by P&L
                system,
            )
        logger.info(
            f"[{system}] released trade #{trade_id} | "
            f"amount=${amount:.2f} pnl=${pnl:.2f} net=${net_return:.2f}"
        )

    async def rebalance(self):
        """
        Weekly rebalance: re-compute 70/30 allocations from current equity.
        Called by a scheduled job (Sunday night or Monday pre-market).
        Phase 1+ — called manually until scheduler is wired.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT system, current_allocation FROM capital_fence"
            )
            total = sum(float(r["current_allocation"]) for r in rows)

            raijin_new  = round(total * 0.70, 2)
            susanoo_new = round(total * 0.30, 2)

            await conn.execute(
                """
                UPDATE capital_fence
                SET current_allocation = CASE system
                    WHEN 'RAIJIN'  THEN $1
                    WHEN 'SUSANOO' THEN $2
                    END,
                capital_available = CASE system
                    WHEN 'RAIJIN'  THEN $1 - capital_in_use
                    WHEN 'SUSANOO' THEN $2 - capital_in_use
                    END,
                updated_at = NOW()
                WHERE system IN ('RAIJIN', 'SUSANOO')
                """,
                raijin_new, susanoo_new,
            )
        logger.info(
            f"Rebalance complete — total equity ${total:.2f} | "
            f"RAIJIN ${raijin_new:.2f} | SUSANOO ${susanoo_new:.2f}"
        )

    async def freeze_system(self, system: str, reason: str):
        """Set is_frozen = True. Logs to circuit_breaker_events."""
        system = system.upper()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE capital_fence
                SET is_frozen = TRUE, freeze_reason = $1, updated_at = NOW()
                WHERE system = $2
                """,
                reason, system,
            )
            await conn.execute(
                """
                INSERT INTO circuit_breaker_events
                    (system, breaker_type, trigger_value, threshold_value, action_taken)
                VALUES ($1, 'CAPITAL_FENCE', NULL, NULL, $2)
                """,
                system, f"FREEZE — {reason}",
            )
        logger.warning(f"🛑 [{system}] frozen: {reason}")

    async def unfreeze_system(self, system: str):
        """Set is_frozen = False. Requires prior manual review."""
        system = system.upper()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE capital_fence
                SET is_frozen = FALSE, freeze_reason = NULL, updated_at = NOW()
                WHERE system = $1
                """,
                system,
            )
            await conn.execute(
                """
                UPDATE circuit_breaker_events
                SET resolved_at = NOW(), notes = 'Manually unfrozen via CapitalFence.unfreeze_system'
                WHERE system = $1 AND breaker_type = 'CAPITAL_FENCE' AND resolved_at IS NULL
                """,
                system,
            )
        logger.info(f"✅ [{system}] unfrozen")

    async def check_and_apply_drawdown_freezes(self):
        """
        Evaluate combined drawdown and freeze systems if thresholds are breached.
        Called after every trade close by the risk orchestrator (Phase 1+).
        """
        dd = await self._combined_drawdown_pct()

        for threshold, systems_to_freeze in FREEZE_THRESHOLDS:
            if dd >= threshold and systems_to_freeze:
                for system in systems_to_freeze:
                    alloc = await self.get_allocation(system)
                    if not alloc["is_frozen"]:
                        await self.freeze_system(
                            system,
                            f"Combined drawdown {dd:.1%} >= {threshold:.0%} threshold",
                        )
                return  # Apply only the highest breached threshold

        if dd >= 0.04:
            logger.warning(f"⚠️  Combined drawdown {dd:.1%} — approaching 6% SUSANOO freeze threshold")

    async def _combined_drawdown_pct(self) -> float:
        """
        Compute combined unrealized drawdown as % of peak equity.
        Phase 0: reads capital_fence allocations as proxy.
        Phase 1+: reads actual IBKR unrealized P&L.
        """
        rows = await self.pool.fetch(
            "SELECT current_allocation, capital_in_use FROM capital_fence"
        )
        total_allocation = sum(float(r["current_allocation"]) for r in rows)
        total_in_use     = sum(float(r["capital_in_use"]) for r in rows)

        # Phase 0 proxy: no real unrealized P&L yet
        # Returns 0.0 until execution layer is wired
        _ = total_in_use
        _ = total_allocation
        return 0.0
