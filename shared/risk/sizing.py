# =============================================================================
# TKO-AGENTS — Shared Risk: Dynamic Kelly Sizer
# File: /Users/shugogeta/tko-agents/RaijinXSusanoo/shared/risk/sizing.py
# =============================================================================

import logging

logger = logging.getLogger("tko-agents.sizing")

# ---------------------------------------------------------------------------
# RAIJIN Kelly tiers — VIX-conditioned
# ---------------------------------------------------------------------------

RAIJIN_KELLY_TIERS: dict[tuple[float, float], float] = {
    (0.0,  20.0): 0.25,   # Low VIX: default capacity
    (20.0, 30.0): 0.15,   # Elevated: compress
    (30.0, 100.0): 0.08,  # Crisis: minimal (VIX_SPIKE breaker → long-only)
}

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
        vrp: float = 0.0,         # VRP for SUSANOO (realized - implied vol)
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

        # Full Kelly: edge / max_loss
        kelly_full = edge_magnitude / max_loss if max_loss > 0 else 0.0

        # VIX tier lookup
        vix_fraction, vix_tier = self._raijin_vix_tier(vix_level)

        # Blended regime score: 50% VIX tier + 50% IVR tier
        ivr_fraction = self._ivr_kelly(underlying_ivr)
        blended      = 0.5 * vix_fraction + 0.5 * ivr_fraction

        # Apply blended fraction to full Kelly
        kelly_fraction = kelly_full * blended

        # Capital to risk: fraction × RAIJIN allocation ($3,500)
        raijin_allocation = 3500.0
        capital_to_risk   = round(kelly_fraction * raijin_allocation, 2)

        # Floor: at least 1 contract, cap at allocation
        capital_to_risk = max(capital_to_risk, max_loss)       # 1 contract min
        capital_to_risk = min(capital_to_risk, raijin_allocation * 0.15)  # 15% cap per position

        contracts = max(1, int(capital_to_risk / max_loss))

        rationale = (
            f"RAIJIN | VIX={vix_level:.1f} → tier {vix_tier} ({vix_fraction:.2f}) | "
            f"IVR={underlying_ivr:.0f} → {ivr_fraction:.2f} | "
            f"blended={blended:.2f} | "
            f"full_kelly={kelly_full:.3f} → fractional={kelly_fraction:.3f} | "
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

    def _raijin_vix_tier(self, vix: float) -> tuple[float, str]:
        for (lo, hi), fraction in RAIJIN_KELLY_TIERS.items():
            if lo <= vix < hi:
                return fraction, f"{lo:.0f}-{hi:.0f}"
        # VIX >= 100 edge case
        return 0.08, "100+"

    def _ivr_kelly(self, ivr: float) -> float:
        if ivr > 50:
            return 0.25
        elif ivr > 25:
            return 0.15
        else:
            return 0.08

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
        susanoo_allocation = 1500.0

        # Dormancy checks
        if vrp >= -3.0:
            return {
                "kelly_full":       0.0,
                "kelly_fraction":   0.0,
                "vix_tier":         f"VRP={vrp:.1f}",
                "capital_to_risk":  0.0,
                "contracts":        0,
                "sizing_rationale": (
                    f"SUSANOO DORMANT — VRP={vrp:.1f} >= -3.0 (no inversion). "
                    f"Signal rejected at sizing."
                ),
            }
        if vix_level > 40.0:
            return {
                "kelly_full":       0.0,
                "kelly_fraction":   0.0,
                "vix_tier":         f"VIX={vix_level:.1f}",
                "capital_to_risk":  0.0,
                "contracts":        0,
                "sizing_rationale": (
                    f"SUSANOO DORMANT — VIX={vix_level:.1f} > 40 "
                    f"(premium too expensive for long premium). "
                ),
            }

        base_risk = 0.03 * susanoo_allocation   # $45 at $1,500
        max_risk  = 0.05 * susanoo_allocation   # $75 at $1,500

        vrp_mag       = abs(vrp)
        scale         = min(1.0, (vrp_mag - 3.0) / 6.0)   # 0→1 as VRP goes -3→-9
        adjusted_risk = base_risk + (scale * (max_risk - base_risk))

        # VIX ceiling compression
        if vix_level > 35.0:
            adjusted_risk *= 0.5

        adjusted_risk = round(adjusted_risk, 2)

        # Full Kelly
        kelly_full     = edge_magnitude / max_loss if max_loss > 0 else 0.0
        kelly_fraction = adjusted_risk / susanoo_allocation

        contracts = max(1, int(adjusted_risk / max_loss)) if max_loss > 0 else 1

        rationale = (
            f"SUSANOO | VRP={vrp:.1f} scale={scale:.2f} | "
            f"VIX={vix_level:.1f} | "
            f"base=${base_risk:.2f} max=${max_risk:.2f} → adjusted=${adjusted_risk:.2f} | "
            f"contracts={contracts} | "
            f"intraday_flag={intraday}"
        )
        logger.info(rationale)

        return {
            "kelly_full":       round(kelly_full, 4),
            "kelly_fraction":   round(kelly_fraction, 4),
            "vix_tier":         f"VRP={vrp:.1f}",
            "capital_to_risk":  adjusted_risk,
            "contracts":        contracts,
            "sizing_rationale": rationale,
        }
