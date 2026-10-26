"""PnL aggregation, per-trader breakdown, time-of-day stats."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def summarize(closed: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [p for p in closed if p["realized"] > 0]
    losses = [p for p in closed if p["realized"] <= 0]
    net = sum(p["realized"] for p in closed)
    slips = [f["slippage_pct"] for f in fills if f["slippage_pct"] is not None and f["side"] == "buy" and f["source"] == "copy"]
    best = max(closed, key=lambda p: p["realized"], default=None)
    worst = min(closed, key=lambda p: p["realized"], default=None)
    return {
        "net": net, "n": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(closed) if closed else 0.0,
        "avg_win": sum(p["realized"] for p in wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(p["realized"] for p in losses) / len(losses) if losses else 0.0,
        "best": best, "worst": worst,
        "avg_slip": sum(slips) / len(slips) if slips else None,
        "fees": sum((f["fee_native"] or 0) + (f["gas_native"] or 0) for f in fills),
    }


def per_trader(closed: list[dict[str, Any]], fills: list[dict[str, Any]], labels: dict[str, str]) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "slips": []})
    for p in closed:
        key = "manual" if p["source"] == "manual" else (f"group:{p['chain']}" if p["source"] == "group" else p["trader"])
        a = agg[key]
        a["n"] += 1
        a["wins"] += 1 if p["realized"] > 0 else 0
        a["pnl"] += p["realized"]
    for f in fills:
        if f["side"] == "buy" and f["source"] == "copy" and f["slippage_pct"] is not None:
            agg[f["trader"]]["slips"].append(f["slippage_pct"])
    out = []
    for k, a in agg.items():
        if a["n"] == 0:
            continue
        label = "manual" if k == "manual" else ("group buy" if k.startswith("group:") else labels.get(k, k[:4] + ".." + k[-3:]))
        out.append({"key": k, "label": label, "n": a["n"], "win_rate": a["wins"] / a["n"], "pnl": a["pnl"],
                    "slip": sum(a["slips"]) / len(a["slips"]) if a["slips"] else None})
    return sorted(out, key=lambda r: -r["pnl"])


def by_hour(closed: list[dict[str, Any]], tz: ZoneInfo) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {}
    for p in closed:
        h = datetime.fromtimestamp(p["opened_at"], tz).hour
        d = out.setdefault(h, {"n": 0, "pnl": 0.0, "wins": 0})
        d["n"] += 1
        d["pnl"] += p["realized"]
        d["wins"] += 1 if p["realized"] > 0 else 0
    return out


def worst_hours(closed: list[dict[str, Any]], tz: ZoneInfo, min_trades: int = 3, k: int = 3) -> list[int]:
    hrs = [(h, d) for h, d in by_hour(closed, tz).items() if d["n"] >= min_trades and d["pnl"] < 0]
    return [h for h, _ in sorted(hrs, key=lambda x: x[1]["pnl"])[:k]]
