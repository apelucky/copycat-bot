"""Per-user settings with defaults; per-chain overrides live under settings['chains'][chain]."""
from __future__ import annotations

import copy
from typing import Any

from ..chains.registry import CHAIN_ORDER, CHAINS

DEFAULTS: dict[str, Any] = {
    "active_chain": "solana",        # solana|base|ethereum|bsc|robinhood|all
    "mode": "paper",                 # live is a locked placeholder
    "paused": False,
    "copy_mult": 0.25,
    "loss_switch": 2,
    "bench_hours": 24,
    "min_liq_usd": 20000,
    "rug_check": True,
    "max_slippage_pct": 3.0,         # max simulated price impact for our own fill
    "max_copy_cost_pct": 4.0,        # stop copying a trader whose avg entry costs us more than this vs leader
    "health_min": 0.35,
    "health_window": 20,
    "groupbuy": True,
    "group_window_min": 10,
    "group_entry_at": 2,
    "group_scale_pct": 25,
    "advisor": False,
    "manual_alerts": True,           # dev-watch / exit alerts for manual trades
    "manual_auto_sell": False,       # let dev-watch / exit logic auto-sell manual trades
    "pause_worst_hours": False,
    "chains": {},
}


def chain_defaults(chain: str) -> dict[str, float]:
    c = CHAINS[chain]
    return {"max_position": c.default_max_position, "daily_loss_cap": c.default_daily_loss_cap,
            "group_size": c.default_group_size}


def default_settings() -> dict[str, Any]:
    s = copy.deepcopy(DEFAULTS)
    s["chains"] = {c: chain_defaults(c) for c in CHAIN_ORDER}
    return s


def merged(raw: dict[str, Any]) -> dict[str, Any]:
    s = default_settings()
    for k, v in (raw or {}).items():
        if k == "chains" and isinstance(v, dict):
            for c, cv in v.items():
                if c in s["chains"] and isinstance(cv, dict):
                    s["chains"][c].update(cv)
        else:
            s[k] = v
    s["mode"] = "paper"  # LIVE is locked in v1, regardless of stored value
    return s


def active_chains(settings: dict[str, Any], enabled: list[str]) -> list[str]:
    a = settings.get("active_chain", "solana")
    if a == "all":
        return [c for c in CHAIN_ORDER if c in enabled]
    return [a] if a in enabled else []
