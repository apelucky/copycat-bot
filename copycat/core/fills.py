"""Simulated (paper) fills: liquidity-aware price impact, DEX fee and per-chain gas."""
from __future__ import annotations

from dataclasses import dataclass

from ..chains.base import ChainConfig

BASE_SLIPPAGE = 0.003  # latency / MEV drag on top of pool impact


@dataclass
class Fill:
    side: str
    tokens: float
    native: float          # buy: total native spent incl fee+gas; sell: net native received after fee+gas
    price: float           # effective execution price, native per token (before fee/gas)
    impact_pct: float      # simulated slippage vs reference price, %
    fee_native: float
    gas_native: float


def price_impact(trade_native: float, native_usd: float | None, liquidity_usd: float) -> float:
    """Constant-product approximation: impact = x / (pool_side + x), pool_side = liquidity/2."""
    if not native_usd or liquidity_usd <= 0:
        return 0.05
    trade_usd = trade_native * native_usd
    side = liquidity_usd / 2.0
    return trade_usd / (side + trade_usd)


def simulate_buy(spend_native: float, ref_price: float, liquidity_usd: float, native_usd: float | None,
                 cfg: ChainConfig) -> Fill:
    if spend_native <= 0 or ref_price <= 0:
        raise ValueError("spend and price must be positive")
    fee = spend_native * cfg.dex_fee_pct
    slip = BASE_SLIPPAGE + price_impact(spend_native, native_usd, liquidity_usd)
    price = ref_price * (1 + slip)
    tokens = (spend_native - fee) / price
    return Fill("buy", tokens, spend_native + cfg.gas_native, price, slip * 100, fee, cfg.gas_native)


def simulate_sell(tokens: float, ref_price: float, liquidity_usd: float, native_usd: float | None,
                  cfg: ChainConfig) -> Fill:
    if tokens <= 0 or ref_price <= 0:
        raise ValueError("tokens and price must be positive")
    gross_ref = tokens * ref_price
    slip = BASE_SLIPPAGE + price_impact(gross_ref, native_usd, liquidity_usd)
    slip = min(slip, 0.99)
    price = ref_price * (1 - slip)
    gross = tokens * price
    fee = gross * cfg.dex_fee_pct
    net = gross - fee - cfg.gas_native
    return Fill("sell", tokens, net, price, slip * 100, fee, cfg.gas_native)


def slippage_vs_leader(side: str, our_price: float, leader_price: float | None) -> float | None:
    """Positive = we did worse than the leader (paid more / received less), in %."""
    if not leader_price or leader_price <= 0 or our_price <= 0:
        return None
    if side == "buy":
        return (our_price / leader_price - 1) * 100
    return (1 - our_price / leader_price) * 100
