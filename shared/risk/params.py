# =============================================================================
# TKO-AGENTS — Shared Risk: Parameter Loader
# Single source of truth: shared/config/risk_parameters.yaml (owner-locked).
# =============================================================================

from pathlib import Path

import yaml

PARAMS_PATH = Path(__file__).resolve().parents[1] / "config" / "risk_parameters.yaml"

REQUIRED_SECTIONS = (
    "account",
    "raijin_kelly_tiers",
    "raijin_ivr_tiers",
    "susanoo_vrp_sizing",
    "circuit_breakers",
    "capital_fence",
    "trade_limits",
)


def load_params(path: Path = PARAMS_PATH) -> dict:
    """Load and minimally validate the risk parameters. Raises on any defect —
    the risk engine must not start with missing limits."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"{path}: expected a mapping at top level")
    missing = [s for s in REQUIRED_SECTIONS if s not in data]
    if missing:
        raise RuntimeError(f"{path}: missing sections {missing}")
    return data


PARAMS = load_params()
