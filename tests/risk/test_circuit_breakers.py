import asyncio

from shared.risk.circuit_breakers import CircuitBreakerManager
from tests.risk.fakes import FakePool


def run(coro):
    return asyncio.run(coro)


# --- Fix #4: PEAK_TROUGH measures current equity against the running peak ---

def test_peak_trough_quiet_when_equity_at_new_high():
    # Old code: MAX 5000 vs MIN 4600 → "-8%" → full halt at an equity high.
    pool = FakePool({"FROM daily_metrics": {"peak": 5000, "current": 5000}})
    assert run(CircuitBreakerManager(pool).check_peak_to_trough()) is False
    assert pool.executed == []


def test_peak_trough_fires_on_real_drawdown():
    pool = FakePool({"FROM daily_metrics": {"peak": 5000, "current": 4590}})
    assert run(CircuitBreakerManager(pool).check_peak_to_trough()) is True
    assert any("PEAK_TROUGH" in str(args) for _, args in pool.executed)


def test_peak_trough_queries_existing_column():
    pool = FakePool({"FROM daily_metrics": {"peak": 5000, "current": 5000}})
    seen = []
    orig = pool.fetchrow

    async def spy(query, *args):
        seen.append(query)
        return await orig(query, *args)

    pool.fetchrow = spy
    run(CircuitBreakerManager(pool).check_peak_to_trough())
    assert "account_equity" in seen[0] and "closing_equity" not in seen[0]


def test_peak_trough_no_history_is_not_triggered():
    pool = FakePool({"FROM daily_metrics": {"peak": None, "current": None}})
    assert run(CircuitBreakerManager(pool).check_peak_to_trough()) is False
