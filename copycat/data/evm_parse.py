"""Decode ERC-20 Transfer logs touching a wallet into swap events (works for Uniswap/PancakeSwap/Aerodrome
and launchpads alike, since every swap moves tokens in/out of the trader's wallet)."""
from __future__ import annotations

from typing import Any

from ..chains.base import SwapEvent

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"  # WETH Withdrawal(address,uint256)
SWAP_TOPICS = {
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",  # Uniswap/PancakeSwap V2
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",  # Uniswap V3
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",  # Uniswap V4 PoolManager
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83",  # PancakeSwap V3
    "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b",  # Aerodrome / Solidly
}
DUST = 1e-12


def topic_addr(t: str) -> str:
    return "0x" + t[-40:].lower()


def pad_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def group_logs(wallet: str, logs: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """tx_hash -> {token: raw signed delta (int units)} for the wallet."""
    w = wallet.lower()
    out: dict[str, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for lg in logs:
        topics = lg.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        key = (lg["transactionHash"], str(lg.get("logIndex")))
        if key in seen:
            continue
        seen.add(key)
        frm, to = topic_addr(topics[1]), topic_addr(topics[2])
        val = int(lg.get("data") or "0x0", 16)
        tok = lg["address"].lower()
        d = out.setdefault(lg["transactionHash"], {})
        if to == w:
            d[tok] = d.get(tok, 0) + val
        if frm == w:
            d[tok] = d.get(tok, 0) - val
    return out


def classify_evm(chain: str, wallet: str, tx_hash: str, ts: int, raw: dict[str, float], decimals: dict[str, int],
                 quote_tokens: dict[str, float], native_value_sent: float = 0.0, source: str = "live") -> list[SwapEvent]:
    """quote_tokens: token -> native per 1 whole token (wrapped native = 1.0, stables = 1/native_usd)."""
    quote_native = -native_value_sent
    others: dict[str, float] = {}
    for tok, v in raw.items():
        amt = v / (10 ** decimals.get(tok, 18))
        if tok in quote_tokens:
            quote_native += amt * quote_tokens[tok]
        elif abs(amt) > DUST:
            others[tok] = amt
    evs: list[SwapEvent] = []
    for tok, amt in others.items():
        share = 1.0 / len(others)
        if amt > 0:
            if quote_native >= -DUST:
                continue  # received without paying: airdrop/spam/transfer-in, not a buy
            # paid with native ETH/BNB (tx value) or a quote token
            evs.append(SwapEvent(chain, wallet, tx_hash, ts, "buy", tok, amt, max(0.0, -quote_native) * share, source=source))
        elif quote_native > DUST:
            evs.append(SwapEvent(chain, wallet, tx_hash, ts, "sell", tok, -amt, quote_native * share, source=source))
        else:
            # No quote token came back in the logs: either a plain transfer or a sell into native ETH/BNB
            # (paid via internal tx). Caller refines with the receipt (see refine_with_receipt).
            evs.append(SwapEvent(chain, wallet, tx_hash, ts, "transfer_out", tok, -amt, 0.0, source=source))
    return evs


def refine_with_receipt(ev: SwapEvent, receipt: dict[str, Any] | None, wrapped: str) -> SwapEvent:
    """transfer_out → sell if the tx contains a DEX Swap event; native proceeds from WETH Withdrawal."""
    if ev.side != "transfer_out" or not receipt:
        return ev
    logs = receipt.get("logs") or []
    if not any((lg.get("topics") or [""])[0].lower() in SWAP_TOPICS for lg in logs):
        return ev
    wad = sum(int(lg.get("data") or "0x0", 16) for lg in logs
              if (lg.get("topics") or [""])[0].lower() == WITHDRAWAL_TOPIC and lg.get("address", "").lower() == wrapped)
    ev.side = "sell"
    ev.native_amount = wad / 1e18
    return ev
