"""DexScreener public API (no key): prices, liquidity, mcap."""
from __future__ import annotations

import time
from typing import Any

from ..chains.base import TokenInfo
from .http import Http

BASE = "https://api.dexscreener.com"


def best_pair(pairs: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    tok = token.lower()
    cands = [p for p in pairs if (p.get("baseToken") or {}).get("address", "").lower() == tok] or pairs
    if not cands:
        return None
    return max(cands, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def pair_to_info(chain: str, token: str, p: dict[str, Any], native_usd: float | None) -> TokenInfo:
    price_usd = float(p.get("priceUsd") or 0)
    base = p.get("baseToken") or {}
    price_native = price_usd / native_usd if native_usd else float(p.get("priceNative") or 0)
    return TokenInfo(
        chain=chain, address=base.get("address", token), symbol=base.get("symbol", "?"),
        name=base.get("name", "?"), price_usd=price_usd, price_native=price_native,
        liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
        mcap_usd=float(p["marketCap"]) if p.get("marketCap") else (float(p["fdv"]) if p.get("fdv") else None),
        pair_address=p.get("pairAddress"), dex=p.get("dexId"), pair_created_at=p.get("pairCreatedAt"),
        url=p.get("url"),
    )


class DexScreener:
    def __init__(self, http: Http, cache_seconds: int = 20):
        self.http = http
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, list]] = {}

    async def pairs(self, ds_chain: str, token: str) -> list[dict[str, Any]]:
        key = (ds_chain, token.lower())
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        data = await self.http.get(f"{BASE}/tokens/v1/{ds_chain}/{token}")
        pairs = data if isinstance(data, list) else (data or {}).get("pairs") or []
        self._cache[key] = (time.time(), pairs)
        return pairs

    async def search_chains(self, token: str) -> list[str]:
        """Which DexScreener chain ids list this token address (used for 0x auto-detect)."""
        data = await self.http.get(f"{BASE}/latest/dex/tokens/{token}")
        pairs = (data or {}).get("pairs") or []
        tok = token.lower()
        return sorted({p["chainId"] for p in pairs if (p.get("baseToken") or {}).get("address", "").lower() == tok})

    async def native_usd(self, ds_chain: str, wrapped: str) -> float | None:
        if not wrapped:
            return None
        p = best_pair(await self.pairs(ds_chain, wrapped), wrapped)
        return float(p["priceUsd"]) if p and p.get("priceUsd") else None
