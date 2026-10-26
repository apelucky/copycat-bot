"""Turn a Solana transaction's balance deltas into SwapEvents (shared by Helius + public RPC paths)."""
from __future__ import annotations

from typing import Any, Callable

from ..chains.base import SwapEvent
from ..chains.registry import SOL_MINT, USDC_SOL, USDT_SOL

LAMPORTS = 1_000_000_000
STABLES = {USDC_SOL, USDT_SOL}
DUST = 1e-9


def classify(chain: str, wallet: str, sig: str, ts: int, sol_delta: float,
             token_deltas: dict[str, tuple[float, float]], stable_to_native: Callable[[float], float] | None = None,
             source: str = "live") -> list[SwapEvent]:
    """sol_delta: wallet's SOL change (incl. WSOL), fee excluded. token_deltas: mint -> (pre, post)."""
    stable_delta = 0.0
    others: dict[str, tuple[float, float]] = {}
    for mint, (pre, post) in token_deltas.items():
        if mint == SOL_MINT:
            sol_delta += post - pre
        elif mint in STABLES:
            stable_delta += post - pre
        else:
            others[mint] = (pre, post)
    quote_native = sol_delta
    if abs(stable_delta) > DUST and stable_to_native:
        quote_native += stable_to_native(stable_delta)
    events: list[SwapEvent] = []
    moved = {m: v for m, v in others.items() if abs(v[1] - v[0]) > DUST}
    for mint, (pre, post) in moved.items():
        d = post - pre
        share = 1.0 / len(moved)
        if d > 0 and quote_native < -DUST:
            events.append(SwapEvent(chain, wallet, sig, ts, "buy", mint, d, -quote_native * share, pre, source=source))
        elif d < 0 and quote_native > DUST:
            events.append(SwapEvent(chain, wallet, sig, ts, "sell", mint, -d, quote_native * share, pre, source=source))
        elif d < 0:
            events.append(SwapEvent(chain, wallet, sig, ts, "transfer_out", mint, -d, 0.0, pre, source=source))
    return events


def from_rpc_tx(chain: str, wallet: str, tx: dict[str, Any], **kw: Any) -> list[SwapEvent]:
    """Parse a getTransaction(jsonParsed) result."""
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return []
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    sig = tx["transaction"]["signatures"][0]
    sol_delta = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS
        if i == 0:
            sol_delta += meta.get("fee", 0) / LAMPORTS  # fee is not part of the trade price
    deltas: dict[str, list[float]] = {}
    for field, idx in (("preTokenBalances", 0), ("postTokenBalances", 1)):
        for b in meta.get(field) or []:
            if b.get("owner") != wallet:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            deltas.setdefault(b["mint"], [0.0, 0.0])[idx] += amt
    return classify(chain, wallet, sig, int(tx.get("blockTime") or 0), sol_delta,
                    {m: (v[0], v[1]) for m, v in deltas.items()}, **kw)


def from_helius_enhanced(chain: str, wallet: str, tx: dict[str, Any], **kw: Any) -> list[SwapEvent]:
    """Parse one Helius enhanced-transactions item using accountData balance changes."""
    if tx.get("transactionError"):
        return []
    sol_delta = 0.0
    deltas: dict[str, list[float]] = {}
    for acc in tx.get("accountData") or []:
        if acc.get("account") == wallet:
            sol_delta += float(acc.get("nativeBalanceChange") or 0) / LAMPORTS
        for tb in acc.get("tokenBalanceChanges") or []:
            if tb.get("userAccount") != wallet:
                continue
            raw = tb.get("rawTokenAmount") or {}
            amt = float(raw.get("tokenAmount") or 0) / (10 ** int(raw.get("decimals") or 0))
            deltas.setdefault(tb["mint"], [0.0, 0.0])[1] += amt
    if tx.get("feePayer") == wallet:
        sol_delta += float(tx.get("fee") or 0) / LAMPORTS
    # Helius gives deltas, not pre/post: pre unknown -> use (0, delta) and drop pre_balance.
    evs = classify(chain, wallet, tx["signature"], int(tx.get("timestamp") or 0), sol_delta,
                   {m: (0.0, v[1]) if v[1] >= 0 else (-v[1], 0.0) for m, v in deltas.items()}, **kw)
    for e in evs:
        e.pre_balance = None
    return evs
