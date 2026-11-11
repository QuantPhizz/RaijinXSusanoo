from pathlib import Path

import pytest
import yaml

from shared.risk import capital_fence, circuit_breakers, sizing
from shared.risk.params import PARAMS, PARAMS_PATH, load_params

REPO = Path(__file__).resolve().parents[2]


# --- Fix #7: YAML is the single source of risk parameters -------------------

def test_yaml_loads_with_all_sections():
    assert load_params() == yaml.safe_load(PARAMS_PATH.read_text())


def test_missing_section_fails_loudly(tmp_path):
    bad = tmp_path / "risk.yaml"
    bad.write_text("trade_limits:\n  max_risk_dollars: 250\n")
    with pytest.raises(RuntimeError, match="missing sections"):
        load_params(bad)


def test_sizing_reads_yaml():
    lim, fence = PARAMS["trade_limits"], PARAMS["capital_fence"]
    equity = PARAMS["account"]["starting_equity"]
    assert sizing.MAX_RISK_DOLLARS == lim["max_risk_dollars"]
    assert sizing.MAX_POSITION_PCT == lim["max_position_pct"]
    assert sizing.RAIJIN_ALLOCATION == equity * fence["raijin_allocation_pct"]
    assert sizing.SUSANOO_ALLOCATION == equity * fence["susanoo_allocation_pct"]
    assert sorted(sizing.RAIJIN_KELLY_TIERS.values(), reverse=True) == [
        t["fraction"] for t in PARAMS["raijin_kelly_tiers"]
    ]


def test_breakers_read_yaml():
    cb = PARAMS["circuit_breakers"]
    assert circuit_breakers.DAILY_DD_THRESHOLD == -cb["daily_drawdown_pct"]
    assert circuit_breakers.PEAK_TROUGH_THRESHOLD == -cb["peak_to_trough_pct"]
    assert circuit_breakers.VIX_SPIKE_TRIGGER == cb["vix_spike_threshold"]
    assert circuit_breakers.VIX_SPIKE_RESOLVE == cb["vix_resume_threshold"]
    assert circuit_breakers.IS_BUDGET_THRESHOLD == cb["is_budget_pct"]


def test_fence_reads_yaml():
    f = PARAMS["capital_fence"]
    assert [t for t, _ in capital_fence.FREEZE_THRESHOLDS] == [
        f["combined_dd_freeze_all"], f["combined_dd_freeze_susanoo"], f["combined_dd_alert"],
    ]


def test_ivr_tiers_preserved():
    s = sizing.KellySizer()
    assert [s._ivr_kelly(v) for v in (80, 51, 50, 26, 25, 0)] == [
        0.25, 0.25, 0.15, 0.15, 0.08, 0.08,
    ]


def test_bot_config_has_no_duplicate_risk_params():
    text = (REPO / "RAIJIN/python-bot/src/config.py").read_text()
    for name in ("PDT_LIMIT", "MAX_RISK_DOLLARS", "MAX_DAILY_DD", "KELLY_FRACTION"):
        assert name not in text
