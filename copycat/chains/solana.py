"""Solana adapter: Helius enhanced API (if HELIUS_API_KEY) or public RPC balance-delta parsing,
DexScreener prices, RugCheck security, Kolscan/GMGN/seed leaderboards, pump.fun metadata."""
from __future__ import annotations

import logging
from typing import Any

from ..data import leaderboards as lb
from ..data.dexscreener import DexScreener, best_pair, pair_to_info
from ..data.http import Http
from ..data.pumpfun import PumpFun
from ..data.rugcheck import RugCheck
from ..data.solana_parse import LAMPORTS, from_helius_enhanced, from_rpc_tx
from .base import ChainAdapter, ChainConfig, LeaderboardEntry, SecurityReport, SwapEvent, TokenInfo
from .registry import SOL_MINT, address_kind, is_solana_pubkey

log = logging.getLogger(__name__)


class SolanaAdapter(ChainAdapter):
    def __init__(self, cfg: ChainConfig, http: Http, ds: DexScreener, helius_key: str | None,
                 rpc_url: str | None = None, seed_file: str = "seeds/wallets.json"):
        self.cfg = cfg
        self.http = http
        self.ds = ds
        self.helius_key = helius_key
        self.rpc_url = rpc_url or (f"https://mainnet.helius-rpc.com/?api-key={helius_key}" if helius_key else cfg.default_rpc)
        self.rug = RugCheck(http)
        self.pump = PumpFun(http)
        self.seed_file = seed_file
        self._sol_usd: float | None = None

    def status(self) -> tuple[bool, str]:
        if self.helius_key:
            return True, "Solana: Helius enhanced API + RPC"
        return True, "Solana: public RPC (rate-limited; set HELIUS_API_KEY for reliable wallet streaming)"

    def is_valid_address(self, addr: str) -> bool:
        return address_kind(addr) == "solana" and is_solana_pubkey(addr)

    async def rpc(self, method: str, params: list) -> Any:
        d = await self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in d:
            raise RuntimeError(f"RPC {method}: {d['error']}")
        return d.get("result")

    def _stable_to_native(self, amt: float) -> float:
        return amt / self._sol_usd if self._sol_usd else 0.0

    async def poll(self, wallets: list[str], state: dict[str, Any]) -> list[SwapEvent]:
        await self.native_usd()
        out: list[SwapEvent] = []
        for w in wallets:
            if not is_solana_pubkey(w):
                continue
            try:
                out.extend(await self._poll_wallet(w, state))
            except Exception as e:  # noqa: BLE001 - one bad wallet must not stop the loop
                log.warning("solana poll %s failed: %s", w[:6], e)
        return out

    async def _poll_wallet(self, wallet: str, state: dict[str, Any]) -> list[SwapEvent]:
        cursor = state.get(wallet)
        if self.helius_key:
            params = {"api-key": self.helius_key, "limit": 25}
            txs = await self.http.get(f"https://api.helius.xyz/v0/addresses/{wallet}/transactions", params=params)
            if not txs:
                return []
            newest = txs[0]["signature"]
            if cursor is None:
                state[wallet] = newest
                return []
            fresh = []
            for t in txs:
                if t["signature"] == cursor:
                    break
                fresh.append(t)
            state[wallet] = newest
            evs: list[SwapEvent] = []
            for t in reversed(fresh):
                evs.extend(from_helius_enhanced("solana", wallet, t, stable_to_native=self._stable_to_native))
            return evs
        opts: dict[str, Any] = {"limit": 25}
        if cursor:
            opts["until"] = cursor
        sigs = await self.rpc("getSignaturesForAddress", [wallet, opts]) or []
        if not sigs:
            return []
        state[wallet] = sigs[0]["signature"]
        if cursor is None:
            return []
        evs = []
        for s in reversed(sigs):
            if s.get("err"):
                continue
            try:
                tx = await self.rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
                evs.extend(from_rpc_tx("solana", wallet, tx, stable_to_native=self._stable_to_native))
            except Exception as e:  # noqa: BLE001 - skip one bad tx, keep the rest
                log.warning("solana tx %s parse failed: %s", s["signature"][:10], str(e)[:120])
        return evs

    async def native_usd(self) -> float | None:
        try:
            v = await self.ds.native_usd("solana", SOL_MINT)
            if v:
                self._sol_usd = v
        except Exception as e:  # noqa: BLE001
            log.debug("sol price failed: %s", e)
        return self._sol_usd

    async def token_info(self, token: str) -> TokenInfo | None:
        pairs = await self.ds.pairs("solana", token)
        p = best_pair(pairs, token)
        if not p:
            return None
        return pair_to_info("solana", token, p, await self.native_usd())

    async def security_check(self, token: str) -> SecurityReport:
        rep = await self.rug.check(token)
        if not rep.creator:
            coin = await self.pump.coin(token)
            if coin:
                rep.creator = coin.get("creator")
        return rep

    async def leaderboard(self) -> list[LeaderboardEntry]:
        entries: list[LeaderboardEntry] = []
        try:
            entries = await lb.kolscan(self.http, 7)
        except Exception as e:  # noqa: BLE001
            log.warning("kolscan failed: %s", e)
        try:
            entries += await lb.gmgn(self.http, "solana", "sol")
        except Exception as e:  # noqa: BLE001
            log.info("gmgn (sol) unavailable: %s", str(e)[:80])
        return entries + lb.load_seeds(self.seed_file, "solana")

    async def funder_of(self, wallet: str) -> str | None:
        """First SOL sender to the wallet (scans up to 3k signatures back)."""
        before = None
        oldest = None
        for _ in range(3):
            opts: dict[str, Any] = {"limit": 1000}
            if before:
                opts["before"] = before
            sigs = await self.rpc("getSignaturesForAddress", [wallet, opts]) or []
            if not sigs:
                break
            oldest = sigs[-1]["signature"]
            before = oldest
            if len(sigs) < 1000:
                break
        if not oldest:
            return None
        tx = await self.rpc("getTransaction", [oldest, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        return funder_from_tx(wallet, tx)


def funder_from_tx(wallet: str, tx: dict[str, Any] | None) -> str | None:
    if not tx:
        return None
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    meta = tx.get("meta") or {}
    if keys and keys[0] != wallet:
        return keys[0]
    best, drop = None, 0
    for i, k in enumerate(keys):
        if k == wallet:
            continue
        d = meta["preBalances"][i] - meta["postBalances"][i]
        if d > drop:
            best, drop = k, d
    return best if drop > 0.001 * LAMPORTS else None
