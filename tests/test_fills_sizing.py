import pytest

from copycat.chains.registry import CHAINS
from copycat.core.fills import price_impact, simulate_buy, simulate_sell, slippage_vs_leader
from copycat.core.sizing import copy_buy_size, sell_fraction

SOL = CHAINS["solana"]
ETH = CHAINS["ethereum"]


def test_copy_size_quarter_of_leader():
    size, why = copy_buy_size(2.0, 0.25, 0.5, 10, SOL.gas_native)
    assert why is None and size == pytest.approx(0.5)


def test_copy_size_capped_by_max_position():
    size, _ = copy_buy_size(10.0, 0.25, 0.5, 10, SOL.gas_native)
    assert size == 0.5


def test_copy_size_capped_by_balance_and_dust():
    size, _ = copy_buy_size(4.0, 0.25, 1.0, 0.3, SOL.gas_native)
    assert size == pytest.approx(0.3 - 3 * SOL.gas_native)
    size, why = copy_buy_size(0.001, 0.25, 1.0, 10, SOL.gas_native)
    assert size == 0 and "too small" in why


def test_copy_size_unknown_leader_size_uses_half_max():
    size, _ = copy_buy_size(0, 0.25, 0.5, 10, SOL.gas_native)
    assert size == 0.25


def test_sell_fraction_proportional():
    assert sell_fraction(500, 1000, None) == 0.5
    assert sell_fraction(250, None, 1000) == 0.25
    assert sell_fraction(990, 1000, None) == 1.0
    assert sell_fraction(10, None, None) == 1.0


def test_price_impact_grows_with_size():
    small = price_impact(0.1, 150, 100_000)
    big = price_impact(10, 150, 100_000)
    assert 0 < small < big < 1
    assert price_impact(1, None, 1000) == 0.05


def test_buy_fill_fees_gas_and_slippage():
    f = simulate_buy(1.0, 0.001, 300_000, 150, SOL)
    assert f.native == pytest.approx(1.0 + SOL.gas_native)
    assert f.fee_native == pytest.approx(0.01)
    assert f.price > 0.001
    assert f.tokens == pytest.approx(0.99 / f.price)


def test_sell_fill_round_trip_loses_fees():
    b = simulate_buy(1.0, 0.001, 300_000, 150, SOL)
    s = simulate_sell(b.tokens, 0.001, 300_000, 150, SOL)
    assert s.native < b.native  # flat price round trip costs fees + slippage + gas
    assert s.native > 0.95


def test_ethereum_gas_is_material():
    b = simulate_buy(0.02, 0.0001, 1_000_000, 3000, ETH)
    assert b.gas_native / b.native > 0.05


def test_slippage_vs_leader_sign():
    assert slippage_vs_leader("buy", 1.02, 1.0) == pytest.approx(2.0)
    assert slippage_vs_leader("sell", 0.98, 1.0) == pytest.approx(2.0)
    assert slippage_vs_leader("buy", 1.0, None) is None


def test_invalid_inputs():
    with pytest.raises(ValueError):
        simulate_buy(0, 1, 1, 1, SOL)
