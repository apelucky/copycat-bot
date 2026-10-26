"""Copy sizing + proportional sell math."""
from __future__ import annotations


def copy_buy_size(leader_native: float, mult: float, max_position: float, balance: float,
                  gas: float, fallback_frac: float = 0.5) -> tuple[float, str | None]:
    """Return (size, skip_reason). Unknown leader size -> fallback_frac * max_position."""
    size = leader_native * mult if leader_native > 0 else max_position * fallback_frac
    size = min(size, max_position)
    reserve = gas * 3
    if size > balance - reserve:
        size = balance - reserve
    if size <= 0:
        return 0.0, "insufficient paper balance"
    if size < gas * 5:
        return 0.0, f"size {size:.5f} too small vs gas"
    return size, None


def sell_fraction(sold: float, pre_balance: float | None, tracked_holding: float | None) -> float:
    """Share of the leader's bag that was sold; we sell the same share of ours."""
    base = pre_balance if pre_balance and pre_balance > 0 else (tracked_holding if tracked_holding and tracked_holding > 0 else None)
    if base is None:
        return 1.0
    f = sold / base
    if f >= 0.98:
        return 1.0
    return max(0.0, min(1.0, f))
