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


# --- Fix #5: VIX breaker and check_all fail closed ---------------------------

def test_vix_missing_fails_closed():
    pool = FakePool({"raw_payload->>'vix'": None})
    assert run(CircuitBreakerManager(pool).check_vix_spike()) is True
    assert any("VIX data unavailable" in str(args) for _, args in pool.executed)


def test_vix_below_trigger_is_clear():
    pool = FakePool({"raw_payload->>'vix'": {"vix": 18.0}})
    assert run(CircuitBreakerManager(pool).check_vix_spike()) is False


def test_vix_above_trigger_fires():
    pool = FakePool({"raw_payload->>'vix'": {"vix": 31.0}})
    assert run(CircuitBreakerManager(pool).check_vix_spike()) is True


def test_check_all_treats_errors_as_triggered():
    pool = FakePool({
        "FROM trades": RuntimeError("db down"),
        "FROM daily_metrics": {"peak": 5000, "current": 5000},
        "raw_payload->>'vix'": {"vix": 15.0},
        "v_trailing_is_ratio": None,
    })
    assert run(CircuitBreakerManager(pool).check_all("RAIJIN")) == ["DAILY_DD"]


# --- IS_BUDGET reads the column v_trailing_is_ratio actually exposes --------

def test_is_budget_fires_above_threshold():
    pool = FakePool({"v_trailing_is_ratio": {"system": "RAIJIN", "avg_is_pct_of_edge": 0.31}})
    assert run(CircuitBreakerManager(pool).check_is_budget("RAIJIN")) is True


def test_is_budget_quiet_below_threshold():
    pool = FakePool({"v_trailing_is_ratio": {"system": "RAIJIN", "avg_is_pct_of_edge": 0.10}})
    assert run(CircuitBreakerManager(pool).check_is_budget("RAIJIN")) is False


# --- DAILY_DD uses the New York trading date, not the DB (UTC) date ---------

def test_daily_dd_uses_new_york_session_date():
    pool = FakePool({"FROM trades": {"daily_pnl": 0}, "FROM capital_fence": {"total": 5000}})
    seen = []
    orig = pool.fetchrow

    async def spy(query, *args):
        seen.append(query)
        return await orig(query, *args)

    pool.fetchrow = spy
    run(CircuitBreakerManager(pool).check_daily_drawdown())
    assert "America/New_York" in seen[0] and "CURRENT_DATE" not in seen[0]


def test_daily_dd_fires_at_two_percent_loss():
    pool = FakePool({"FROM trades": {"daily_pnl": -100}, "FROM capital_fence": {"total": 5000}})
    assert run(CircuitBreakerManager(pool).check_daily_drawdown()) is True
