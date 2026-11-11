# =============================================================================
# TKO-AGENTS — Shared Risk: Dynamic Kelly Sizer
# File: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/risk/sizing.py
# =============================================================================

import logging

from shared.risk.params import PARAMS

logger = logging.getLogger("tko-agents.sizing")

# ---------------------------------------------------------------------------
# Parameters — all values come from risk_parameters.yaml
# ---------------------------------------------------------------------------

RAIJIN_KELLY_TIERS: dict[tuple[float, float], float] = {
    (float(t["vix_range"][0]), float(t["vix_range"][1])): float(t["fraction"])
    for t in PARAMS["raijin_kelly_tiers"]
}
RAIJIN_IVR_TIERS: list[tuple[float | None, float]] = [
    (None if t["min_ivr"] is None else float(t["min_ivr"]), float(t["fraction"]))
    for t in PARAMS["raijin_ivr_tiers"]
]

_equity = float(PARAMS["account"]["starting_equity"])
_fence  = PARAMS["capital_fence"]
_limits = PARAMS["trade_limits"]
_vrp    = PARAMS["susanoo_vrp_sizing"]

RAIJIN_ALLOCATION  = round(_equity * float(_fence["raijin_allocation_pct"]), 2)
SUSANOO_ALLOCATION = round(_equity * float(_fence["susanoo_allocation_pct"]), 2)
MAX_RISK_DOLLARS   = float(_limits["max_risk_dollars"])   # hard per-trade cap
MAX_POSITION_PCT   = float(_limits["max_position_pct"])

SUSANOO_BASE_PCT       = float(_vrp["base_pct"])
SUSANOO_MAX_PCT        = float(_vrp["max_pct"])
SUSANOO_VRP_ACTIVATION = float(_vrp["vrp_activation"])
SUSANOO_VRP_FULL_SCALE = float(_vrp["vrp_full_scale"])
SUSANOO_VIX_HALF       = float(_vrp["vix_half_threshold"])
SUSANOO_VIX_DORMANT    = float(_vrp["vix_dormant_threshold"])


def compute_vrp(implied_vol: float, realized_vol: float) -> float:
    """
    Variance Risk Premium, in vol points: VRP = IV − RV.
    Positive → premium rich (RAIJIN sells). Negative → premium cheap
    (SUSANOO buys when VRP < -3.0). Matches SUSANOO v3 and signals.vrp_value.
    """
    return implied_vol - realized_vol

# ---------------------------------------------------------------------------
# Kelly Sizer
# ---------------------------------------------------------------------------

class KellySizer:
    """
    VIX-conditioned dynamic fractional Kelly sizing.
    Different schedules for RAIJIN (premium selling) and SUSANOO (long premium).

    PDT NOTE: Slot conservation is NOT a sizing input.
    PDT rule eliminated June 4 2026. Frequency governor removed.
    The Kelly fraction and capital fence are now the sole frequency governors.
    Circuit breakers (DAILY_DD, PEAK_TROUGH) act as hard frequency caps.

    Strategy context:
    RAIJIN  — credit spreads, defined risk, theta decay. Wins at 65-75%.
    SUSANOO — debit spreads, directional, long premium. Wins at 40-50%.
    """

    def compute_kelly(
        self,
        system: str,
        edge_magnitude: float,    # Expected edge in $ per contract
        max_loss: float,          # Max loss per contract (spread width - credit)
        vix_level: float,         # Current VIX
        underlying_ivr: float,    # Underlying IV rank 0-100
        vrp: float = 0.0,         # VRP = IV - RV (see compute_vrp); SUSANOO needs < -3
        intraday: bool = False,   # SUSANOO intraday flag (Phase S1)
    ) -> dict:
        """
        Returns sizing decision:
        {
            "kelly_full":       float,   # Unconstrained Kelly fraction
            "kelly_fraction":   float,   # After VIX conditioning
            "vix_tier":         str,     # Which VIX tier applied
            "capital_to_risk":  float,   # $ to risk (floored at 1 contract)
            "contracts":        int,     # Number of contracts
            "sizing_rationale": str      # Human-readable explanation
        }
        """
        system = system.upper()

        if system == "RAIJIN":
            return self._size_raijin(
                edge_magnitude, max_loss, vix_level, underlying_ivr
            )
        elif system == "SUSANOO":
            return self._size_susanoo(
                edge_magnitude, max_loss, vix_level, vrp, intraday
            )
        else:
            raise ValueError(f"Unknown system: {system}")

    # -----------------------------------------------------------------------
    # RAIJIN sizing
    # -----------------------------------------------------------------------

    def _size_raijin(
        self,
        edge_magnitude: float,
        max_loss: float,
        vix_level: float,
        underlying_ivr: float,
    ) -> dict:
        if max_loss <= 0:
            raise ValueError("max_loss must be > 0")

        # No positive expected value → no trade. Never floor a negative Kelly up.
        if edge_magnitude <= 0:
            return self._reject(
                "n/a",
                f"RAIJIN REJECTED — edge_magnitude={edge_magnitude:.2f} <= 0 (no positive EV).",
            )

        # Full Kelly: edge / max_loss
        kelly_full = edge_magnitude / max_loss

        # VIX tier lookup
        vix_fraction, vix_tier = self._raijin_vix_tier(vix_level)

        # Blended regime score: 50% VIX tier + 50% IVR tier
        ivr_fraction = self._ivr_kelly(underlying_ivr)
        blended      = 0.5 * vix_fraction + 0.5 * ivr_fraction

        # Apply blended fraction to full Kelly
        kelly_fraction = kelly_full * blended

        # Capital to risk: fraction × RAIJIN allocation
        kelly_budget = round(kelly_fraction * RAIJIN_ALLOCATION, 2)

        # Hard caps are never overridden: locked $ limit and per-position cap.
        hard_cap = min(MAX_RISK_DOLLARS, RAIJIN_ALLOCATION * MAX_POSITION_PCT)
        if max_loss > hard_cap:
            return self._reject(
                vix_tier,
                f"RAIJIN REJECTED — max_loss=${max_loss:.2f} per contract exceeds "
                f"hard cap ${hard_cap:.2f}.",
            )

        # 1-contract floor applies to the Kelly budget only, inside the hard cap.
        budget          = min(max(kelly_budget, max_loss), hard_cap)
        contracts       = int(budget // max_loss)
        capital_to_risk = round(contracts * max_loss, 2)

        rationale = (
            f"RAIJIN | VIX={vix_level:.1f} → tier {vix_tier} ({vix_fraction:.2f}) | "
            f"IVR={underlying_ivr:.0f} → {ivr_fraction:.2f} | "
            f"blended={blended:.2f} | "
            f"full_kelly={kelly_full:.3f} → fractional={kelly_fraction:.3f} | "
            f"kelly_budget=${kelly_budget:.2f} hard_cap=${hard_cap:.2f} | "
            f"capital_to_risk=${capital_to_risk:.2f} | "
            f"contracts={contracts}"
        )
        logger.info(rationale)

        return {
            "kelly_full":       round(kelly_full, 4),
            "kelly_fraction":   round(kelly_fraction, 4),
            "vix_tier":         vix_tier,
            "capital_to_risk":  capital_to_risk,
            "contracts":        contracts,
            "sizing_rationale": rationale,
        }

    @staticmethod
    def _reject(tier: str, rationale: str) -> dict:
        """Zero-size decision. Callers must treat contracts == 0 as 'do not trade'."""
        logger.info(rationale)
        return {
            "kelly_full":       0.0,
            "kelly_fraction":   0.0,
            "vix_tier":         tier,
            "capital_to_risk":  0.0,
            "contracts":        0,
            "sizing_rationale": rationale,
        }

    def _raijin_vix_tier(self, vix: float) -> tuple[float, str]:
        for (lo, hi), fraction in RAIJIN_KELLY_TIERS.items():
            if lo <= vix < hi:
                return fraction, f"{lo:.0f}-{hi:.0f}"
        # VIX beyond the last tier: use the most conservative fraction
        return min(RAIJIN_KELLY_TIERS.values()), "100+"

    def _ivr_kelly(self, ivr: float) -> float:
        for min_ivr, fraction in RAIJIN_IVR_TIERS:
            if min_ivr is None or ivr > min_ivr:
                return fraction
        return min(f for _, f in RAIJIN_IVR_TIERS)

    # -----------------------------------------------------------------------
    # SUSANOO sizing — continuous VRP function
    # -----------------------------------------------------------------------

    def _size_susanoo(
        self,
        edge_magnitude: float,
        max_loss: float,
        vix_level: float,
        vrp: float,
        intraday: bool,
    ) -> dict:
        """
        Continuous sizing based on VRP (Variance Risk Premium).
        SUSANOO is dormant if VRP >= -3.0 (no meaningful inversion).

        Intraday flag (Phase S1): no sizing change — intraday debit spreads
        use identical Kelly logic. The flag is passed to IS checkpoints only.
        """
        susanoo_allocation = SUSANOO_ALLOCATION

        if max_loss <= 0:
            raise ValueError("max_loss must be > 0")

        # Dormancy checks
        if vrp >= SUSANOO_VRP_ACTIVATION:
            return self._reject(
                f"VRP={vrp:.1f}",
                f"SUSANOO DORMANT — VRP={vrp:.1f} >= {SUSANOO_VRP_ACTIVATION} (no inversion). "
                f"Signal rejected at sizing.",
            )
        if vix_level > SUSANOO_VIX_DORMANT:
            return self._reject(
                f"VIX={vix_level:.1f}",
                f"SUSANOO DORMANT — VIX={vix_level:.1f} > {SUSANOO_VIX_DORMANT} "
                f"(premium too expensive for long premium). ",
            )
        if edge_magnitude <= 0:
            return self._reject(
                f"VRP={vrp:.1f}",
                f"SUSANOO REJECTED — edge_magnitude={edge_magnitude:.2f} <= 0 (no positive EV).",
            )

        base_risk = SUSANOO_BASE_PCT * susanoo_allocation   # $45 at $1,500
        max_risk  = SUSANOO_MAX_PCT * susanoo_allocation    # $75 at $1,500

        # 0→1 as VRP goes activation → full_scale (-3 → -9)
        scale = min(1.0, (SUSANOO_VRP_ACTIVATION - vrp)
                         / (SUSANOO_VRP_ACTIVATION - SUSANOO_VRP_FULL_SCALE))
        adjusted_risk = base_risk + (scale * (max_risk - base_risk))

        # VIX ceiling compression
        if vix_level > SUSANOO_VIX_HALF:
            adjusted_risk *= 0.5

        adjusted_risk = round(min(adjusted_risk, MAX_RISK_DOLLARS), 2)

        # Full Kelly
        kelly_full     = edge_magnitude / max_loss
        kelly_fraction = adjusted_risk / susanoo_allocation

        # The VRP risk budget is the per-trade limit: no 1-contract floor.
        contracts = int(adjusted_risk // max_loss)
        if contracts == 0:
            return self._reject(
                f"VRP={vrp:.1f}",
                f"SUSANOO REJECTED — max_loss=${max_loss:.2f} per contract exceeds "
                f"risk budget ${adjusted_risk:.2f}.",
            )
        capital_to_risk = round(contracts * max_loss, 2)

        rationale = (
            f"SUSANOO | VRP={vrp:.1f} scale={scale:.2f} | "
            f"VIX={vix_level:.1f} | "
            f"base=${base_risk:.2f} max=${max_risk:.2f} → adjusted=${adjusted_risk:.2f} | "
            f"capital_to_risk=${capital_to_risk:.2f} | "
            f"contracts={contracts} | "
            f"intraday_flag={intraday}"
        )
        logger.info(rationale)

        return {
            "kelly_full":       round(kelly_full, 4),
            "kelly_fraction":   round(kelly_fraction, 4),
            "vix_tier":         f"VRP={vrp:.1f}",
            "capital_to_risk":  capital_to_risk,
            "contracts":        contracts,
            "sizing_rationale": rationale,
        }
