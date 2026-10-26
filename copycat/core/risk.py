"""Risk gate used by copy, group and manual entries."""
from __future__ import annotations

from dataclasses import dataclass

from ..chains.base import SecurityReport, TokenInfo


@dataclass
class RiskInput:
    paused: bool
    chain_paused: bool
    realized_today: float
    daily_loss_cap: float
    size: float
    max_position: float
    token: TokenInfo | None
    min_liq_usd: float
    rug_check: bool
    security: SecurityReport | None
    impact_pct: float | None
    max_slippage_pct: float
    worst_hour: bool = False
    manual: bool = False


def check(r: RiskInput) -> str | None:
    """Return skip reason or None if allowed."""
    if r.paused:
        return "bot paused"
    if r.chain_paused or (r.daily_loss_cap > 0 and -r.realized_today >= r.daily_loss_cap):
        return "daily loss cap hit"
    if r.worst_hour and not r.manual:
        return "paused during worst hour"
    if r.size > r.max_position + 1e-12:
        return f"size {r.size:g} > max position {r.max_position:g}"
    if r.token is None:
        return "no price/liquidity data"
    if r.token.liquidity_usd < r.min_liq_usd:
        return f"liquidity ${r.token.liquidity_usd:,.0f} < ${r.min_liq_usd:,.0f}"
    if r.rug_check and r.security is not None and not r.security.ok:
        return "rug-check: " + (", ".join(r.security.reasons[:3]) or "failed")
    if r.impact_pct is not None and r.impact_pct > r.max_slippage_pct:
        return f"slippage {r.impact_pct:.1f}% > {r.max_slippage_pct:g}%"
    return None
