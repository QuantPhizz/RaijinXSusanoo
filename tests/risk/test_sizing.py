import pytest

from shared.risk.sizing import KellySizer


@pytest.fixture
def sizer():
    return KellySizer()


def raijin(sizer, **kw):
    args = dict(edge_magnitude=60, max_loss=200, vix_level=15, underlying_ivr=60)
    args.update(kw)
    return sizer.compute_kelly("RAIJIN", **args)


def susanoo(sizer, **kw):
    args = dict(edge_magnitude=20, max_loss=40, vix_level=20, underlying_ivr=10, vrp=-6.0)
    args.update(kw)
    return sizer.compute_kelly("SUSANOO", **args)


# --- Fix #1: non-positive edge never trades ---------------------------------

@pytest.mark.parametrize("edge", [-40.0, 0.0])
def test_raijin_non_positive_edge_sizes_to_zero(sizer, edge):
    r = raijin(sizer, edge_magnitude=edge)
    assert r["contracts"] == 0
    assert r["capital_to_risk"] == 0.0


@pytest.mark.parametrize("edge", [-50.0, 0.0])
def test_susanoo_non_positive_edge_sizes_to_zero(sizer, edge):
    r = susanoo(sizer, edge_magnitude=edge)
    assert r["contracts"] == 0
    assert r["capital_to_risk"] == 0.0


@pytest.mark.parametrize("system", ["RAIJIN", "SUSANOO"])
def test_non_positive_max_loss_is_rejected(sizer, system):
    with pytest.raises(ValueError):
        sizer.compute_kelly(system, edge_magnitude=10, max_loss=0, vix_level=15,
                            underlying_ivr=50, vrp=-6.0)


def test_raijin_positive_edge_still_trades(sizer):
    assert raijin(sizer)["contracts"] >= 1


def test_susanoo_dormant_above_activation(sizer):
    assert susanoo(sizer, vrp=-2.9)["contracts"] == 0


# --- Fixes #2/#3: hard caps are never overridden ----------------------------

def test_raijin_large_edge_capped_at_max_risk_dollars(sizer):
    r = raijin(sizer, edge_magnitude=200, max_loss=100)
    assert r["capital_to_risk"] <= 250
    assert r["contracts"] == 2


def test_raijin_contract_wider_than_hard_cap_is_rejected(sizer):
    r = raijin(sizer, edge_magnitude=60, max_loss=300)
    assert r["contracts"] == 0
    assert r["capital_to_risk"] == 0.0


def test_raijin_one_contract_floor_kept_within_hard_cap(sizer):
    # Kelly budget ~$44 < one $200 contract: floor to 1, still under $250.
    r = raijin(sizer, edge_magnitude=10, max_loss=200)
    assert r["contracts"] == 1
    assert r["capital_to_risk"] == 200


def test_raijin_capital_to_risk_is_actual_risk(sizer):
    r = raijin(sizer, edge_magnitude=200, max_loss=110)
    assert r["capital_to_risk"] == r["contracts"] * 110


def test_susanoo_contract_wider_than_budget_is_rejected(sizer):
    r = susanoo(sizer, edge_magnitude=10, max_loss=300)
    assert r["contracts"] == 0


def test_susanoo_never_exceeds_vrp_budget(sizer):
    r = susanoo(sizer, max_loss=25, vrp=-9.0)  # budget $75 → 3 contracts
    assert r["contracts"] == 3
    assert r["capital_to_risk"] == 75
