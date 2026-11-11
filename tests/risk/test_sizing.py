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
