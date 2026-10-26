"""Shared EVM adapter (Base, Ethereum, BSC, Robinhood Chain). Wallet swaps are detected from ERC-20
Transfer logs via JSON-RPC eth_getLogs (public RPC by default, override with RPC_URL_<CHAIN> e.g. an
Alchemy/QuickNode URL). Prices: DexScreener. Security: GoPlus. Leaderboard: GMGN (if reachable) + seeds.
Funding source: Etherscan v2 (ETHERSCAN_API_KEY) or Blockscout where available."""
from __future__ import annotations

import logging
import time
from typing import Any

from ..data import leaderboards as lb
from ..data.dexscreener import DexScreener, best_pair, pair_to_info
from ..data.evm_parse import TRANSFER_TOPIC, classify_evm, group_logs, pad_topic, refine_with_receipt
from ..data.goplus import GoPlus
from ..data.http import Http
from .base import ChainAdapter, ChainConfig, LeaderboardEntry, SecurityReport, SwapEvent, TokenInfo
from .registry import address_kind

log = logging.getLogger(__name__)

BLOCKSCOUT = {
    "ethereum": "https://eth.blockscout.com/api",
    "base": "https://base.blockscout.com/api",
    "robinhood": "https://robinhoodchain.blockscout.com/api",
}
MAX_RANGE = {"ethereum": 30, "base": 150, "bsc": 300, "robinhood": 3000}   # max catch-up per poll
CHUNK = {"robinhood": 500}  # eth_getLogs block span per request (free public RPCs cap at ~50)


class EvmAdapter(ChainAdapter):
    def __init__(self, cfg: ChainConfig, http: Http, ds: DexScreener, rpc_url: str | None = None,
                 etherscan_key: str | None = None, seed_file: str = "seeds/wallets.json"):
        self.cfg = cfg
        self.http = http
        self.ds = ds
        self.rpcs = ([rpc_url] if rpc_url else []) + [cfg.default_rpc, *cfg.fallback_rpcs]
        self.rpc_url = self.rpcs[0]
        self.custom_rpc = rpc_url is not None
        self.etherscan_key = etherscan_key
        self.goplus = GoPlus(http)
        self.seed_file = seed_file
        self._decimals: dict[str, int] = {}
        self._native_usd: float | None = None
        self.wrapped = cfg.wrapped_native.lower()
        self.disabled_reason: str | None = cfg.disabled_reason

    def status(self) -> tuple[bool, str]:
        if self.disabled_reason:
            return False, f"{self.cfg.name}: disabled — {self.disabled_reason}"
        src = "custom RPC" if self.custom_rpc else "public RPC (rate-limited; set RPC_URL_" + self.cfg.key.upper() + ")"
        return True, f"{self.cfg.name}: {src}"

    def is_valid_address(self, addr: str) -> bool:
        return address_kind(addr) == "evm"

    async def rpc(self, method: str, params: list) -> Any:
        """JSON-RPC with failover across configured endpoints (public RPCs differ in eth_getLogs limits)."""
        last: Exception | None = None
        for _ in range(len(self.rpcs)):
            try:
                d = await self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                if "error" in d:
                    raise RuntimeError(f"RPC {method}: {d['error']}")
                return d.get("result")
            except Exception as e:  # noqa: BLE001
                last = e
                if len(self.rpcs) > 1:
                    i = self.rpcs.index(self.rpc_url)
                    self.rpc_url = self.rpcs[(i + 1) % len(self.rpcs)]
                    log.info("%s: RPC failover → %s (%s)", self.cfg.key, self.rpc_url, str(e)[:80])
        raise last or RuntimeError("rpc failed")

    def _learn_wrapped(self, pairs: list[dict[str, Any]]) -> None:
        """Robinhood Chain: learn the WETH address from pairs' quote tokens (not hard-coded)."""
        if self.wrapped:
            return
        for p in pairs:
            q = p.get("quoteToken") or {}
            if q.get("symbol") == "WETH" and q.get("address", "").startswith("0x") and int(q["address"], 16) != 0:
                self.wrapped = q["address"].lower()
                return

    async def native_usd(self) -> float | None:
        try:
            if self.cfg.native_ref:
                v = await self.ds.native_usd(*self.cfg.native_ref)
            else:
                v = await self.ds.native_usd(self.cfg.dexscreener_id, self.wrapped)
            if v:
                self._native_usd = v
        except Exception as e:  # noqa: BLE001
            log.debug("native price failed: %s", e)
        return self._native_usd

    async def decimals(self, token: str) -> int:
        if token not in self._decimals:
            try:
                r = await self.rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
                self._decimals[token] = int(r, 16) if r and r != "0x" else 18
            except Exception:  # noqa: BLE001
                self._decimals[token] = 18
        return self._decimals[token]

    def quote_map(self) -> dict[str, float]:
        q = {self.wrapped: 1.0} if self.wrapped else {}
        if self._native_usd:
            for s in self.cfg.stables:
                q[s.lower()] = 1.0 / self._native_usd
        return q

    async def poll(self, wallets: list[str], state: dict[str, Any]) -> list[SwapEvent]:
        if not wallets or self.disabled_reason:
            return []
        latest = int(await self.rpc("eth_blockNumber", []), 16)
        last = state.get("block")
        if last is None:
            state["block"] = latest
            return []
        start = int(last) + 1
        if start > latest:
            return []
        rng = MAX_RANGE.get(self.cfg.key, 300)
        if latest - start > rng:
            start = latest - rng  # stay current rather than lag behind
        await self.native_usd()
        padded = [pad_topic(w) for w in wallets]
        logs: list[dict] = []
        step = CHUNK.get(self.cfg.key, 50)
        for lo in range(start, latest + 1, step):
            hi = min(latest, lo + step - 1)
            try:
                chunk: list[dict] = []
                for topics in ([TRANSFER_TOPIC, padded], [TRANSFER_TOPIC, None, padded]):
                    chunk += await self.rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi), "topics": topics}]) or []
            except Exception:
                if lo == start:
                    raise  # nothing fetched: let the caller back off; cursor unchanged
                break      # keep what we have; resume from here next poll
            logs += chunk
            state["block"] = hi
        events: list[SwapEvent] = []
        ts_cache: dict[str, int] = {}
        for w in wallets:
            for txh, raw in group_logs(w, logs).items():
                for tok in raw:
                    await self.decimals(tok)
                value = 0.0
                if any(v > 0 for t, v in raw.items() if t not in self.quote_map()):
                    try:
                        tx = await self.rpc("eth_getTransactionByHash", [txh])
                        if tx and tx.get("from", "").lower() == w.lower():
                            value = int(tx.get("value") or "0x0", 16) / 1e18
                    except Exception:  # noqa: BLE001
                        pass
                ts = ts_cache.setdefault(txh, int(time.time()))
                evs = classify_evm(self.cfg.key, w, txh, ts, raw, self._decimals, self.quote_map(), value)
                if any(e.side == "transfer_out" for e in evs):
                    try:
                        receipt = await self.rpc("eth_getTransactionReceipt", [txh])
                    except Exception:  # noqa: BLE001
                        receipt = None
                    evs = [refine_with_receipt(e, receipt, self.wrapped) for e in evs]
                events.extend(evs)
        return events

    async def token_info(self, token: str) -> TokenInfo | None:
        pairs = await self.ds.pairs(self.cfg.dexscreener_id, token)
        self._learn_wrapped(pairs)
        p = best_pair(pairs, token)
        if not p:
            return None
        return pair_to_info(self.cfg.key, token, p, await self.native_usd())

    async def security_check(self, token: str) -> SecurityReport:
        if not self.cfg.goplus_id:
            return SecurityReport(ok=False, reasons=["no security provider for chain"])
        return await self.goplus.check(self.cfg.goplus_id, token)

    async def leaderboard(self) -> list[LeaderboardEntry]:
        entries: list[LeaderboardEntry] = []
        if self.cfg.gmgn_id:
            try:
                entries = lb.parse_gmgn(self.cfg.key, await self.http.get(
                    f"https://gmgn.ai/defi/quotation/v1/rank/{self.cfg.gmgn_id}/wallets/7d",
                    params={"orderby": "pnl_7d", "direction": "desc"}))
                usd = await self.native_usd()
                for e in entries:
                    if e.extra.get("pnl_is_usd") and usd:
                        e.pnl_native = e.pnl_native / usd
            except Exception as e:  # noqa: BLE001
                log.info("gmgn (%s) unavailable: %s", self.cfg.key, str(e)[:80])
        return entries + lb.load_seeds(self.seed_file, self.cfg.key)

    async def funder_of(self, wallet: str) -> str | None:
        params = {"module": "account", "action": "txlist", "address": wallet, "sort": "asc", "page": 1, "offset": 1}
        try:
            if self.etherscan_key and self.cfg.chain_id:
                d = await self.http.get("https://api.etherscan.io/v2/api",
                                        params={**params, "chainid": self.cfg.chain_id, "apikey": self.etherscan_key})
            elif self.cfg.key in BLOCKSCOUT:
                d = await self.http.get(BLOCKSCOUT[self.cfg.key], params=params)
            else:
                return None
            res = d.get("result")
            if isinstance(res, list) and res:
                frm = res[0].get("from", "").lower()
                return frm if frm and frm != wallet.lower() else None
        except Exception as e:  # noqa: BLE001
            log.debug("funder lookup failed: %s", e)
        return None
