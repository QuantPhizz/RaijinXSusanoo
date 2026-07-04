# =============================================================================
# TKO-AGENTS — Shared Risk Engine
# Package: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/risk/__init__.py
#
# Five modules — call order in risk orchestrator (Phase 1+):
#   1. capital_fence    — allocation check + freeze state
#   2. circuit_breakers — account + market + system breakers
#   3. conflict         — cross-system Greek exposure overlap
#   4. sizing           — VIX-conditioned fractional Kelly
#   5. intraday_tracker — same-day close observability (replaces pdt)
#
# PDT rule eliminated June 4 2026 (FINRA Rule 4210 amended).
# pdt.py removed. intraday_tracker.py is the replacement.
# No slot arbitration. No day-trade count limit.
# Binding intraday constraint: IBKR real-time margin engine.
# =============================================================================

from .capital_fence      import CapitalFence
from .circuit_breakers   import CircuitBreakerManager
from .conflict           import ConflictDetector
from .sizing             import KellySizer
from .intraday_tracker   import IntradayTracker, is_same_trading_day, is_market_hours

__all__ = [
    "CapitalFence",
    "CircuitBreakerManager",
    "ConflictDetector",
    "KellySizer",
    "IntradayTracker",
    "is_same_trading_day",
    "is_market_hours",
]
