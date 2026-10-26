"""Bot/bait filtering, rank score and rolling trader health."""
from __future__ import annotations

import math
from typing import Any

from ..chains.base import LeaderboardEntry


def flag_wallet(e: LeaderboardEntry, min_trades: int = 30) -> str | None:
    """Return a reason string if the wallet should not be copied."""
    t, wr = e.trades, e.win_rate
    if t > 3000:
        return "bot (huge trade count)"
    if t > 500 and abs(e.pnl_native) / t < 0.01:
        return "wash pattern (many trades, ~no PnL)"
    if wr >= 0.97 and t >= 10:
        return "bait (~100% win rate)"
    if wr >= 0.9 and t < min_trades:
        return "bait (high win rate, tiny sample)"
    if t < min_trades:
        return f"too few trades (<{min_trades})"
    if e.pnl_native <= 0:
        return "negative PnL"
    return None


def rank_score(e: LeaderboardEntry) -> float:
    pnl_c = math.log10(1 + max(e.pnl_native, 0))
    confidence = min(1.0, e.trades / 100)
    return round(pnl_c * (0.5 + e.win_rate) * (0.5 + 0.5 * confidence) * 100, 2)


def rank(entries: list[LeaderboardEntry], min_trades: int = 30) -> list[dict[str, Any]]:
    """Dedupe by wallet (keep best source), flag, score, rank. Flagged rows are returned with rank None."""
    by_wallet: dict[str, LeaderboardEntry] = {}
    for e in entries:
        cur = by_wallet.get(e.wallet)
        if cur is None or (e.source != "seed" and e.trades > cur.trades):
            by_wallet[e.wallet] = e
    rows = []
    for e in by_wallet.values():
        flagged = None if e.source == "seed" and e.trades == 0 else flag_wallet(e, min_trades)
        rows.append({"wallet": e.wallet, "label": e.label, "pnl": e.pnl_native, "wins": e.wins, "losses": e.losses,
                     "trades": e.trades, "score": rank_score(e) if e.trades else 1.0, "source": e.source, "flagged": flagged})
    ok = sorted([r for r in rows if not r["flagged"]], key=lambda r: -r["score"])
    for i, r in enumerate(ok, 1):
        r["rank"] = i
    bad = [r for r in rows if r["flagged"]]
    for r in bad:
        r["rank"] = None
    return ok + bad


def health(closed: list[dict[str, Any]]) -> tuple[float | None, int]:
    """Rolling health (0..1) from our last N closed copies of a trader: 60% win rate + 40% avg return."""
    n = len(closed)
    if n == 0:
        return None, 0
    wins = sum(1 for p in closed if p["realized"] > 0)
    rets = [p["realized"] / p["initial_cost"] for p in closed if p.get("initial_cost")]
    avg = sum(rets) / len(rets) if rets else 0.0
    h = 0.6 * (wins / n) + 0.4 * max(0.0, min(1.0, 0.5 + avg))
    return round(h, 3), n
