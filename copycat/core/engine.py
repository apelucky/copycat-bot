"""Copy engine: turns chain events into paper fills, handles auto-switch, Group Buy, exit/dev alerts,
manual trades and risk controls. All money is simulated."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from ..bot import ui
from ..bot.notifier import Notifier
from ..chains.base import ChainAdapter, SecurityReport, SwapEvent, TokenInfo
from ..chains.registry import CHAIN_ORDER, CHAINS, EVM_CHAINS, address_kind
from ..config import AppConfig
from ..data.xsearch import MentionReport, XSearch
from . import ranking, risk, stats
from .fills import Fill, simulate_buy, simulate_sell, slippage_vs_leader
from .groupbuy import GroupBuy, GroupBuyDetector, scale_out_steps
from .settings import active_chains, default_settings, merged
from .sizing import copy_buy_size, sell_fraction

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: AppConfig, repo: Any, adapters: dict[str, ChainAdapter], notifier: Notifier,
                 xsearch: XSearch | None = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.repo = repo
        self.adapters = adapters
        self.notifier = notifier
        self.x = xsearch
        self.clock = clock
        self.groups = GroupBuyDetector()
        self.groups.load(repo.kv_get("groupbuy", []))
        self._info_cache: dict[tuple[str, str], tuple[float, TokenInfo | None]] = {}
        self._sec_cache: dict[tuple[str, str], tuple[float, SecurityReport]] = {}
        self._mention_cache: dict[str, tuple[float, MentionReport]] = {}
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ basics
    def now(self) -> int:
        return int(self.clock())

    def enabled_chains(self) -> list[str]:
        out = []
        for c in CHAIN_ORDER:
            a = self.adapters.get(c)
            if a and a.status()[0] and (self.cfg.enabled_chains_override is None or c in self.cfg.enabled_chains_override):
                out.append(c)
        return out

    def chain_status(self) -> dict[str, str]:
        out = {}
        for c in CHAIN_ORDER:
            a = self.adapters.get(c)
            out[c] = a.status()[1] if a else f"{CHAINS[c].name}: not configured"
        return out

    def settings(self, uid: int) -> dict[str, Any]:
        return merged(self.repo.get_settings(uid))

    def save_settings(self, uid: int, s: dict[str, Any]) -> None:
        self.repo.save_settings(uid, s)

    def ensure_user(self, uid: int, chat_id: int | None = None) -> bool:
        new = self.repo.upsert_user(uid, chat_id or uid, default_settings())
        for c in CHAIN_ORDER:
            self.repo.ensure_account(uid, c, CHAINS[c].start_balance)
        return new

    def day_start(self, days_ago: int = 0) -> int:
        d = datetime.fromtimestamp(self.now(), self.cfg.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return int((d - timedelta(days=days_ago)).timestamp())

    def realized_since(self, uid: int, chain: str, since: int) -> float:
        return sum((f["pnl_native"] or 0) for f in self.repo.fills(uid, since, chain) if f["side"] == "sell")

    def label_of(self, chain: str, wallet: str) -> str:
        for r in self.repo.leaderboard(chain):
            if r["wallet"] == wallet:
                return r["label"]
        f = self.repo.kv_get(f"label:{chain}:{wallet}")
        return f or ui.short(wallet)

    # ------------------------------------------------------------------ market data (cached)
    async def token_info(self, chain: str, token: str, max_age: int = 15) -> TokenInfo | None:
        k = (chain, token)
        hit = self._info_cache.get(k)
        if hit and self.now() - hit[0] < max_age:
            return hit[1]
        try:
            info = await self.adapters[chain].token_info(token)
        except Exception as e:  # noqa: BLE001
            log.warning("token_info %s %s: %s", chain, token[:8], e)
            info = hit[1] if hit else None
        self._info_cache[k] = (self.now(), info)
        return info

    async def security(self, chain: str, token: str) -> SecurityReport:
        k = (chain, token)
        hit = self._sec_cache.get(k)
        if hit and self.now() - hit[0] < 600:
            return hit[1]
        try:
            rep = await self.adapters[chain].security_check(token)
        except Exception as e:  # noqa: BLE001
            rep = SecurityReport(ok=False, reasons=[f"security check error: {str(e)[:60]}"])
        self._sec_cache[k] = (self.now(), rep)
        return rep

    async def native_usd(self, chain: str) -> float | None:
        try:
            return await self.adapters[chain].native_usd()
        except Exception:  # noqa: BLE001
            return None

    async def mentions(self, symbol: str | None, token: str) -> MentionReport:
        if not self.x or not self.x.enabled:
            return MentionReport(False, note="X mentions disabled (no X_BEARER_TOKEN)")
        hit = self._mention_cache.get(token)
        if hit and self.now() - hit[0] < 300:
            return hit[1]
        rep = await self.x.mentions(symbol, token)
        self._mention_cache[token] = (self.now(), rep)
        return rep

    # ------------------------------------------------------------------ leaderboard / following
    def tracked_wallets(self, chain: str) -> list[str]:
        top = [r["wallet"] for r in self.repo.leaderboard(chain) if not r["flagged"]][: self.cfg.tracked_top_n]
        extra = self.repo.all_followed(chain) + list(self.repo.kv_get(f"tracked:{chain}", []))
        return list(dict.fromkeys(top + extra))

    def dev_wallets(self, chain: str) -> dict[str, list[str]]:
        return self.repo.kv_get(f"devwatch:{chain}", {})

    def watch_list(self, chain: str) -> list[str]:
        return list(dict.fromkeys(self.tracked_wallets(chain) + list(self.dev_wallets(chain).keys())))

    async def refresh_leaderboard(self, chain: str) -> list[dict[str, Any]]:
        entries = await self.adapters[chain].leaderboard()
        rows = ranking.rank(entries)
        if rows:
            self.repo.replace_leaderboard(chain, rows, self.now())
        for u in self.repo.list_users():
            uid = u["user_id"]
            if chain in active_chains(self.settings(uid), self.enabled_chains()) and not self.repo.get_following(uid, chain):
                nxt = self.pick_next(uid, chain)
                if nxt:
                    self.repo.set_following(uid, chain, nxt["wallet"], nxt["label"], self.now())
                    await self.notifier.send(uid, f"{ui.badge(chain)} 👑 Now following <b>{ui.safe(nxt['label'])}</b> "
                                                  f"(#{nxt['rank']}) — {ui.short(nxt['wallet'])}")
        return rows

    def is_benched(self, uid: int, chain: str, wallet: str) -> bool:
        return self.repo.trader_state(uid, chain, wallet)["benched_until"] > self.now()

    def pick_next(self, uid: int, chain: str, exclude: set[str] | None = None) -> dict[str, Any] | None:
        exclude = exclude or set()
        for r in self.repo.leaderboard(chain):
            if r["flagged"] or r["wallet"] in exclude or self.is_benched(uid, chain, r["wallet"]):
                continue
            return r
        return None

    def follow(self, uid: int, chain: str, wallet: str, label: str | None = None) -> None:
        label = label or self.label_of(chain, wallet)
        self.repo.kv_set(f"label:{chain}:{wallet}", label)
        self.repo.set_following(uid, chain, wallet, label, self.now())
        self.repo.save_trader_state(uid, chain, wallet, benched_until=0, consec_losses=0, bench_reason=None)

    async def switch_trader(self, uid: int, chain: str, wallet: str, reason: str, detail: str = "") -> dict[str, Any] | None:
        s = self.settings(uid)
        until = self.now() + int(s["bench_hours"] * 3600)
        self.repo.save_trader_state(uid, chain, wallet, benched_until=until, bench_reason=reason, consec_losses=0)
        nxt = self.pick_next(uid, chain, {wallet})
        old_label = self.label_of(chain, wallet)
        self.repo.set_following(uid, chain, nxt["wallet"] if nxt else None, nxt["label"] if nxt else None, self.now())
        self.repo.kv_set(f"undo:{uid}:{chain}", {"from": wallet, "to": nxt["wallet"] if nxt else None, "ts": self.now()})
        hist = self.repo.kv_get(f"switches:{uid}", [])
        hist.append({"chain": chain, "from": wallet, "to": nxt["wallet"] if nxt else None, "reason": reason, "ts": self.now()})
        self.repo.kv_set(f"switches:{uid}", hist[-50:])
        until_s = ui.fmt_ts(until, self.cfg.tz, "%H:%M %d %b")
        body = (f"{ui.badge(chain)} <b>🔁 TRADER SWITCHED: {ui.safe(reason, 40)}</b>\n{detail}"
                f"<pre>{ui.safe(old_label)} → BENCHED {s['bench_hours']}h (until {until_s} WAT)\n")
        if nxt:
            body += (f"Now following → {ui.safe(nxt['label'])} (#{nxt['rank']})\n"
                     f"7d PnL {nxt['pnl']:+.0f} · Win {nxt['wins'] / max(nxt['trades'], 1):.0%} · {nxt['trades']} trades</pre>")
        else:
            body += "No eligible trader left — copying idle. /follow one manually.</pre>"
        await self.notifier.send(uid, body, ui.kb([("🏆 Leaderboard", "m:lb"), ("↩️ Undo switch", f"undo:{chain}")]))
        return nxt

    async def undo_switch(self, uid: int, chain: str) -> str:
        u = self.repo.kv_get(f"undo:{uid}:{chain}")
        if not u:
            return "Nothing to undo."
        self.follow(uid, chain, u["from"])
        self.repo.kv_set(f"undo:{uid}:{chain}", None)
        return f"↩️ Switch undone — following <b>{ui.safe(self.label_of(chain, u['from']))}</b> again (streak reset)."

    # ------------------------------------------------------------------ fills
    async def _buy(self, uid: int, chain: str, token: str, size: float, info: TokenInfo, source: str,
                   trader: str | None, leader_price: float | None = None, sig: str | None = None,
                   meta: dict[str, Any] | None = None, reason: str = "") -> tuple[dict[str, Any], Fill, float | None]:
        cfg = CHAINS[chain]
        fill = simulate_buy(size, info.price_native, info.liquidity_usd, await self.native_usd(chain), cfg)
        slip = slippage_vs_leader("buy", fill.price, leader_price)
        pos = self.repo.find_open_position(uid, chain, token, source)
        if pos and (source != "copy" or pos["trader"] == trader):
            self.repo.update_position(pos["id"], tokens=pos["tokens"] + fill.tokens, cost_native=pos["cost_native"] + fill.native,
                                      initial_tokens=pos["initial_tokens"] + fill.tokens, initial_cost=pos["initial_cost"] + fill.native,
                                      fees=pos["fees"] + fill.fee_native + fill.gas_native)
            pid = pos["id"]
        else:
            pid = self.repo.open_position(user_id=uid, chain=chain, token=token, symbol=info.symbol, source=source, trader=trader,
                                          tokens=fill.tokens, cost_native=fill.native, initial_tokens=fill.tokens,
                                          initial_cost=fill.native, fees=fill.fee_native + fill.gas_native,
                                          opened_at=self.now(), meta=meta or {})
        self.repo.add_balance(uid, chain, -fill.native)
        self.repo.add_fill(user_id=uid, chain=chain, position_id=pid, side="buy", token=token, symbol=info.symbol, source=source,
                           trader=trader, tokens=fill.tokens, native=fill.native, price=fill.price, leader_price=leader_price,
                           slippage_pct=slip, impact_pct=fill.impact_pct, fee_native=fill.fee_native, gas_native=fill.gas_native,
                           pnl_native=None, ts=self.now(), leader_sig=sig, reason=reason)
        await self._register_dev_watch(chain, token)
        return self.repo.get_position(pid), fill, slip  # type: ignore[return-value]

    async def _sell(self, uid: int, pos: dict[str, Any], tokens: float, info: TokenInfo | None, reason: str,
                    leader_price: float | None = None, sig: str | None = None) -> tuple[Fill, float, bool, float | None]:
        chain = pos["chain"]
        cfg = CHAINS[chain]
        tokens = min(tokens, pos["tokens"])
        # no price data → assume the token went to ~zero (rugged / delisted)
        ref = info.price_native if info and info.price_native > 0 else 1e-18
        liq = info.liquidity_usd if info else 0.0
        fill = simulate_sell(tokens, ref, liq, await self.native_usd(chain), cfg)
        proceeds = max(fill.native, 0.0)
        frac = tokens / pos["tokens"] if pos["tokens"] else 1.0
        cost_part = pos["cost_native"] * frac
        pnl = proceeds - cost_part
        remaining = pos["tokens"] - tokens
        closed = remaining <= pos["initial_tokens"] * 1e-6
        self.repo.update_position(pos["id"], tokens=0.0 if closed else remaining, cost_native=0.0 if closed else pos["cost_native"] - cost_part,
                                  realized=pos["realized"] + pnl, fees=pos["fees"] + fill.fee_native + fill.gas_native,
                                  status="closed" if closed else "open", closed_at=self.now() if closed else None)
        self.repo.add_balance(uid, chain, proceeds)
        slip = slippage_vs_leader("sell", fill.price, leader_price)
        self.repo.add_fill(user_id=uid, chain=chain, position_id=pos["id"], side="sell", token=pos["token"], symbol=pos["symbol"],
                           source=pos["source"], trader=pos["trader"], tokens=tokens, native=proceeds, price=fill.price,
                           leader_price=leader_price, slippage_pct=slip, impact_pct=fill.impact_pct, fee_native=fill.fee_native,
                           gas_native=fill.gas_native, pnl_native=pnl, ts=self.now(), leader_sig=sig, reason=reason)
        if closed:
            await self._on_closed(uid, self.repo.get_position(pos["id"]))  # type: ignore[arg-type]
        await self._check_loss_cap(uid, chain)
        return fill, pnl, closed, slip

    async def _on_closed(self, uid: int, pos: dict[str, Any]) -> None:
        if pos["source"] != "copy" or not pos["trader"]:
            return
        chain, wallet = pos["chain"], pos["trader"]
        s = self.settings(uid)
        st = self.repo.trader_state(uid, chain, wallet)
        consec = 0 if pos["realized"] > 0 else st["consec_losses"] + 1
        self.repo.save_trader_state(uid, chain, wallet, consec_losses=consec)
        following = self.repo.get_following(uid, chain)
        if not following or following["wallet"] != wallet:
            return
        if consec >= s["loss_switch"]:
            last = self.repo.closed_positions(uid, chain=chain, trader=wallet, source="copy", limit=consec)
            detail = "<pre>" + "\n".join(f"✗ {ui.safe(p['symbol'], 10):<10} {ui.amt(p['realized'], chain, True)}" for p in reversed(last)) + "</pre>"
            await self.switch_trader(uid, chain, wallet, f"{consec} losses in a row", detail)
            return
        h, n = ranking.health(self.repo.closed_positions(uid, chain=chain, trader=wallet, source="copy", limit=s["health_window"]))
        if h is not None and n >= 8 and h < s["health_min"]:
            await self.switch_trader(uid, chain, wallet, "cold trader (health)",
                                     f"Health {h:.2f} &lt; {s['health_min']} over last {n} copies\n")

    async def _check_loss_cap(self, uid: int, chain: str) -> None:
        s = self.settings(uid)
        cap = s["chains"][chain]["daily_loss_cap"]
        today = self.realized_since(uid, chain, self.day_start())
        key = f"capped:{uid}:{chain}"
        day = datetime.fromtimestamp(self.now(), self.cfg.tz).strftime("%Y-%m-%d")
        if cap > 0 and -today >= cap and self.repo.kv_get(key) != day:
            self.repo.kv_set(key, day)
            await self.notifier.send(uid, f"{ui.badge(chain)} 🛑 <b>DAILY LOSS CAP HIT</b>\nRealized today {ui.amt(today, chain, True)} "
                                          f"(cap {ui.amt(cap, chain)}). Auto-copying paused on {CHAINS[chain].name} until midnight WAT.")

    def chain_capped(self, uid: int, chain: str) -> bool:
        day = datetime.fromtimestamp(self.now(), self.cfg.tz).strftime("%Y-%m-%d")
        return self.repo.kv_get(f"capped:{uid}:{chain}") == day

    def in_worst_hour(self, uid: int, s: dict[str, Any]) -> bool:
        if not s.get("pause_worst_hours"):
            return False
        closed = self.repo.closed_positions(uid, since_ts=self.now() - 30 * 86400)
        hour = datetime.fromtimestamp(self.now(), self.cfg.tz).hour
        return hour in stats.worst_hours(closed, self.cfg.tz)

    async def _risk(self, uid: int, chain: str, size: float, info: TokenInfo | None, sec: SecurityReport | None,
                    manual: bool = False, extra_cost: float = 0.0) -> str | None:
        s = self.settings(uid)
        cs = s["chains"][chain]
        impact = None
        if info and info.price_native > 0 and size > 0:
            impact = simulate_buy(size, info.price_native, info.liquidity_usd, await self.native_usd(chain), CHAINS[chain]).impact_pct
        return risk.check(risk.RiskInput(
            paused=s["paused"] and not manual, chain_paused=self.chain_capped(uid, chain),
            realized_today=self.realized_since(uid, chain, self.day_start()), daily_loss_cap=cs["daily_loss_cap"],
            size=size + extra_cost, max_position=cs["max_position"], token=info, min_liq_usd=s["min_liq_usd"],
            rug_check=s["rug_check"], security=sec, impact_pct=impact, max_slippage_pct=s["max_slippage_pct"],
            worst_hour=self.in_worst_hour(uid, s), manual=manual))

    async def _skip(self, uid: int, chain: str, token: str, symbol: str | None, trader: str | None, reason: str, notify: bool) -> None:
        self.repo.add_skip(uid, chain, token, symbol, trader, reason, self.now())
        if notify:
            await self.notifier.send(uid, f"{ui.badge(chain)} ⏭ <b>SKIPPED</b> {ui.safe(symbol)} from {ui.safe(self.label_of(chain, trader) if trader else 'manual')}: {ui.safe(reason, 80)}")

    # ------------------------------------------------------------------ dev watch
    async def _register_dev_watch(self, chain: str, token: str) -> None:
        sec = await self.security(chain, token)
        if not sec.creator:
            return
        dw = self.dev_wallets(chain)
        toks = dw.setdefault(sec.creator, [])
        if token not in toks:
            toks.append(token)
            self.repo.kv_set(f"devwatch:{chain}", dw)

    # ------------------------------------------------------------------ event entry point
    async def on_event(self, ev: SwapEvent) -> None:
        async with self.lock:
            if not self.repo.record_leader_event(ev.__dict__):
                return
            before = self.repo.leader_holding(ev.chain, ev.wallet, ev.token)
            if ev.side == "buy":
                self.repo.set_leader_holding(ev.chain, ev.wallet, ev.token, before + ev.token_amount)
            else:
                base = ev.pre_balance if ev.pre_balance is not None else before
                self.repo.set_leader_holding(ev.chain, ev.wallet, ev.token, base - ev.token_amount)
            try:
                await self._dev_watch(ev)
                await self._copy(ev, before)
                if ev.wallet in self.tracked_wallets(ev.chain):
                    await self._group(ev)
                    await self._exit_alerts(ev, before)
            except Exception:  # noqa: BLE001
                log.exception("event handling failed for %s", ev.signature)

    async def _copy(self, ev: SwapEvent, before: float) -> None:
        followers = self.repo.followers_of(ev.chain, ev.wallet)
        if not followers or ev.side == "transfer_out" and not followers:
            return
        info = await self.token_info(ev.chain, ev.token)
        if info and not ev.symbol:
            ev.symbol = info.symbol
        sec = await self.security(ev.chain, ev.token) if ev.side == "buy" else None
        leader_price = ev.price_native
        if leader_price is None and info:
            leader_price = info.price_native
        for uid in followers:
            s = self.settings(uid)
            if ev.chain not in active_chains(s, self.enabled_chains()):
                continue
            if ev.side == "buy":
                await self._copy_buy(uid, ev, info, sec, leader_price, s)
            elif ev.side == "sell":
                await self._copy_sell(uid, ev, info, leader_price, before)

    async def _copy_buy(self, uid: int, ev: SwapEvent, info: TokenInfo | None, sec: SecurityReport | None,
                        leader_price: float | None, s: dict[str, Any]) -> None:
        chain = ev.chain
        cs = s["chains"][chain]
        existing = self.repo.find_open_position(uid, chain, ev.token, "copy")
        already = existing["cost_native"] if existing and existing["trader"] == ev.wallet else 0.0
        leader_native = ev.native_amount or (ev.token_amount * info.price_native if info else 0.0)
        size, why = copy_buy_size(leader_native, s["copy_mult"], cs["max_position"] - already,
                                  self.repo.get_balance(uid, chain), CHAINS[chain].gas_native)
        if why:
            await self._skip(uid, chain, ev.token, ev.symbol, ev.wallet, why, notify=True)
            return
        reason = await self._risk(uid, chain, size, info, sec, extra_cost=already)
        if reason:
            await self._skip(uid, chain, ev.token, ev.symbol, ev.wallet, reason, notify=True)
            return
        assert info is not None
        pos, fill, slip = await self._buy(uid, chain, ev.token, size, info, "copy", ev.wallet, leader_price, ev.signature)
        label = self.label_of(chain, ev.wallet)
        text = (f"{ui.badge(chain)} <b>🟢 COPY BUY</b> {ui.PAPER}\nLeader <b>{ui.safe(label)}</b> bought <b>${ui.sym(info.symbol)}</b>"
                f"<pre>Leader size   {ui.amt(leader_native, chain)} @ {ui.price(leader_price or 0)}\n"
                f"Our size      {ui.amt(size, chain)}  ({s['copy_mult']}x)\n"
                f"Our fill      {ui.price(fill.price)}\n"
                f"Slippage      {ui.pct(slip)} vs leader\n"
                f"Fees+gas      {ui.amt(fill.fee_native + fill.gas_native, chain, dp=5)}\n"
                f"Liquidity     {ui.usd(info.liquidity_usd)} ✓   Rug-check {'✓' if (sec and sec.ok) else '—'}</pre>")
        await self.notifier.send(uid, text, ui.kb([("🔍 Chart", f"url:{info.url or ''}"), ("❌ Close now", f"close:{pos['id']}")]))
        await self._check_copy_cost(uid, chain, ev.wallet, s)

    async def _check_copy_cost(self, uid: int, chain: str, wallet: str, s: dict[str, Any]) -> None:
        slips = [f["slippage_pct"] for f in self.repo.fills(uid, self.now() - 14 * 86400, chain)
                 if f["side"] == "buy" and f["source"] == "copy" and f["trader"] == wallet and f["slippage_pct"] is not None][-10:]
        if len(slips) >= 5:
            avg = sum(slips) / len(slips)
            following = self.repo.get_following(uid, chain)
            if avg > s["max_copy_cost_pct"] and following and following["wallet"] == wallet:
                await self.switch_trader(uid, chain, wallet, "copy delay too costly",
                                         f"Avg entry {avg:+.2f}% worse than leader over {len(slips)} copies (limit {s['max_copy_cost_pct']}%)\n")

    async def _copy_sell(self, uid: int, ev: SwapEvent, info: TokenInfo | None, leader_price: float | None, before: float) -> None:
        pos = self.repo.find_open_position(uid, ev.chain, ev.token, "copy")
        if not pos or pos["trader"] != ev.wallet:
            return
        frac = sell_fraction(ev.token_amount, ev.pre_balance, before)
        tokens = pos["tokens"] if frac >= 1.0 else pos["tokens"] * frac
        if tokens <= 0:
            return
        fill, pnl, closed, slip = await self._sell(uid, pos, tokens, info, "leader sold", leader_price, ev.signature)
        chain = ev.chain
        cost_part = fill.native - pnl
        text = (f"{ui.badge(chain)} <b>🔴 COPY SELL</b> {ui.PAPER}\nLeader sold <b>{frac:.0%}</b> of ${ui.sym(pos['symbol'])} → we sold <b>{frac:.0%}</b>"
                f"<pre>Sold          {ui.amt(cost_part, chain)} cost basis\n"
                f"Exit fill     {ui.price(fill.price)}\n"
                f"Slippage      {ui.pct(slip)} vs leader\n"
                f"Realized PnL  {ui.amt(pnl, chain, True)} ({(pnl / cost_part * 100 if cost_part else 0):+.1f}%)\n"
                f"Remaining     {'closed' if closed else f'{1 - frac:.0%} still open'}</pre>")
        await self.notifier.send(uid, text, ui.kb([("🖼 Trade card", f"tcard:{pos['id']}")]))

    # ------------------------------------------------------------------ group buy
    async def _funder(self, chain: str, wallet: str) -> str | None:
        known, f = self.repo.get_funder(chain, wallet)
        if known:
            return f
        try:
            f = await asyncio.wait_for(self.adapters[chain].funder_of(wallet), timeout=15)
        except Exception:  # noqa: BLE001
            f = None
        self.repo.set_funder(chain, wallet, f, self.now())
        return f

    def _group_users(self, chain: str) -> list[int]:
        out = []
        for u in self.repo.list_users():
            s = self.settings(u["user_id"])
            if s["groupbuy"] and chain in active_chains(s, self.enabled_chains()):
                out.append(u["user_id"])
        return out

    async def _group(self, ev: SwapEvent) -> None:
        if ev.side == "buy":
            funder = await self._funder(ev.chain, ev.wallet)
            g, event = self.groups.on_buy(ev.chain, ev.token, ev.wallet, ev.timestamp or self.now(), ev.native_amount, funder)
            self.repo.kv_set("groupbuy", self.groups.dump())
            if event:
                await self._group_signal(g, event)
        else:
            g, early = self.groups.on_sell(ev.chain, ev.token, ev.wallet)
            if g:
                self.repo.kv_set("groupbuy", self.groups.dump())
            if early and g:
                for uid in self._group_users(ev.chain):
                    pos = self.repo.find_open_position(uid, ev.chain, ev.token, "group")
                    if pos:
                        info = await self.token_info(ev.chain, ev.token)
                        _, pnl, _, _ = await self._sell(uid, pos, pos["tokens"], info, "early group buyer sold")
                        await self.notifier.send(uid, f"{ui.badge(ev.chain)} <b>👥 GROUP EXIT</b> {ui.PAPER}\nEarly buyer "
                                                      f"<b>{ui.safe(self.label_of(ev.chain, ev.wallet))}</b> sold ${ui.sym(pos['symbol'])} → full exit\n"
                                                      f"Realized {ui.amt(pnl, ev.chain, True)}")

    async def _group_signal(self, g: GroupBuy, event: str) -> None:
        chain = g.chain
        info = await self.token_info(chain, g.token)
        sym = info.symbol if info else "?"
        ment = await self.mentions(sym, g.token)
        lines = []
        for i, b in enumerate(g.buys, 1):
            dt = b["ts"] - g.first_ts
            lines.append(f"{i}. {ui.safe(self.label_of(chain, b['wallet']), 14):<14} {ui.amt(b['native'], chain) if b['native'] else '   ?':>10} +{dt // 60}m{dt % 60:02d}s")
        shared = g.shared_funders()
        warn = ""
        if shared:
            warn = "\n⚠️ Shared funder: " + "; ".join(f"{ui.short(f)} → {', '.join(ui.safe(self.label_of(chain, w), 10) for w in ws)}" for f, ws in shared.items())
            warn += " (counted once)"
        xline = (f"\nX: {ment.count} mentions · weighted {ment.weighted} · 15m {ment.velocity_15m} vs {ment.baseline_15m}"
                 f"{' 🚀 SPIKE' if ment.spike else ''}" + (f" · {', '.join(ment.notable)}" if ment.notable else "")) if ment.enabled else f"\nX: {ment.note}"
        title = "GROUP BUY" if event == "formed" else f"GROUP BUY +{g.effective_count}"
        head = (f"{ui.badge(chain)} <b>👥 {title}</b> {ui.PAPER}\n<b>{g.effective_count}</b> independent tracked wallets bought "
                f"<b>${ui.sym(sym)}</b> <code>{ui.short(g.token)}</code><pre>" + "\n".join(lines) + f"</pre>{warn}{xline}")
        for uid in self._group_users(chain):
            s = self.settings(uid)
            pos = self.repo.find_open_position(uid, chain, g.token, "group")
            note = ""
            if event == "formed" and not pos and g.effective_count >= s["group_entry_at"]:
                size = min(s["chains"][chain]["group_size"], self.repo.get_balance(uid, chain) - CHAINS[chain].gas_native * 3)
                sec = await self.security(chain, g.token)
                reason = await self._risk(uid, chain, size, info, sec) if size > 0 else "insufficient paper balance"
                if reason:
                    await self._skip(uid, chain, g.token, sym, None, f"group buy: {reason}", notify=False)
                    note = f"\n⏭ Not entered: {ui.safe(reason, 80)}"
                else:
                    assert info is not None
                    pos, fill, _ = await self._buy(uid, chain, g.token, size, info, "group", None,
                                                   meta={"group": GroupBuyDetector.key(chain, g.token), "steps": 0})
                    note = f"\n✅ Paper-entered {ui.amt(size, chain)} @ {ui.price(fill.price)}"
            elif pos and event == "joined":
                target = scale_out_steps(g.effective_count, s["group_entry_at"], ment.spike)
                done = int(pos["meta"].get("steps", 0))
                if target > done:
                    frac = s["group_scale_pct"] / 100 * (target - done)
                    tokens = min(pos["tokens"], pos["initial_tokens"] * frac)
                    _, pnl, closed, _ = await self._sell(uid, pos, tokens, info, f"group scale-out step {target}")
                    if not closed:
                        meta = self.repo.get_position(pos["id"])["meta"]  # type: ignore[index]
                        meta["steps"] = target
                        self.repo.update_position(pos["id"], meta=meta)
                    note = f"\n📤 Scaled out {frac:.0%} of entry (step {target}{', X spike' if ment.spike else ''}) → {ui.amt(pnl, chain, True)}"
            buttons = ui.kb([("🔎 Token card", f"card:{chain}:{g.token}"[:64])])
            await self.notifier.send(uid, head + note, buttons)

    # ------------------------------------------------------------------ exit + dev alerts
    async def _exit_alerts(self, ev: SwapEvent, before: float) -> None:
        if ev.side not in ("sell", "transfer_out"):
            return
        positions = [p for p in self.repo.open_positions(chain=ev.chain) if p["token"] == ev.token]
        if not positions:
            return
        frac = sell_fraction(ev.token_amount, ev.pre_balance, before)
        label = self.label_of(ev.chain, ev.wallet)
        for p in positions:
            if p["source"] == "copy" and p["trader"] == ev.wallet:
                continue  # mirrored by copy engine
            if p["source"] == "group" and ev.wallet in self.groups.groups.get(GroupBuyDetector.key(ev.chain, ev.token), GroupBuy(ev.chain, ev.token, 0)).earliest(2):
                continue  # handled by group exit
            s = self.settings(p["user_id"])
            if p["source"] == "manual" and not s["manual_alerts"]:
                continue
            verb = "sold" if ev.side == "sell" else "moved out"
            await self.notifier.send(p["user_id"], f"{ui.badge(ev.chain)} ⚠️ <b>TRADER EXIT</b>: tracked wallet <b>{ui.safe(label)}</b> "
                                                   f"{verb} {frac:.0%} of ${ui.sym(p['symbol'])} — you hold it ({p['source']}).",
                                     ui.kb([("Sell 50%", f"sellp:{p['id']}:50"), ("Sell 100%", f"sellp:{p['id']}:100")]))

    async def _dev_watch(self, ev: SwapEvent) -> None:
        if ev.side not in ("sell", "transfer_out"):
            return
        toks = self.dev_wallets(ev.chain).get(ev.wallet, [])
        if ev.token not in toks:
            return
        info = await self.token_info(ev.chain, ev.token, max_age=0)
        for p in [p for p in self.repo.open_positions(chain=ev.chain) if p["token"] == ev.token]:
            s = self.settings(p["user_id"])
            what = "sold" if ev.side == "sell" else "transferred out"
            if p["source"] != "manual" or s["manual_auto_sell"]:
                _, pnl, _, _ = await self._sell(p["user_id"], p, p["tokens"], info, f"dev {what}")
                await self.notifier.send(p["user_id"], f"{ui.badge(ev.chain)} 🚨 <b>DEV {what.upper()}</b> ${ui.sym(p['symbol'])} "
                                                       f"(creator {ui.short(ev.wallet)}) → sold 100% (paper). Realized {ui.amt(pnl, ev.chain, True)}")
            elif s["manual_alerts"]:
                await self.notifier.send(p["user_id"], f"{ui.badge(ev.chain)} 🚨 <b>DEV {what.upper()}</b> ${ui.sym(p['symbol'])} "
                                                       f"(creator {ui.short(ev.wallet)}). Manual position NOT auto-sold.",
                                         ui.kb([("Sell 50%", f"sellp:{p['id']}:50"), ("Sell 100%", f"sellp:{p['id']}:100")]))

    # ------------------------------------------------------------------ manual trading
    async def resolve_chain(self, uid: int, token: str) -> tuple[str | None, list[str]]:
        """Return (chain, candidates). Base58 → solana. 0x → DexScreener lookup across enabled EVM chains."""
        kind = address_kind(token)
        enabled = self.enabled_chains()
        if kind == "solana":
            return ("solana" if "solana" in enabled else None), ["solana"]
        if kind != "evm":
            return None, []
        evm = [c for c in EVM_CHAINS if c in enabled]
        found: list[str] = []
        try:
            ds = await self.adapters[evm[0]].ds.search_chains(token) if evm else []  # type: ignore[attr-defined]
            found = [c for c in evm if CHAINS[c].dexscreener_id in ds]
        except Exception:  # noqa: BLE001
            pass
        active = self.settings(uid)["active_chain"]
        if len(found) == 1:
            return found[0], found
        if active in found:
            return active, found
        return None, found or evm

    async def manual_buy(self, uid: int, chain: str, token: str, size: float) -> str:
        if chain not in self.enabled_chains():
            return f"{CHAINS[chain].name} is disabled: {self.chain_status()[chain]}"
        if not self.adapters[chain].is_valid_address(token):
            return "That doesn't look like a valid contract address for this chain."
        info = await self.token_info(chain, token, max_age=0)
        sec = await self.security(chain, token)
        existing = self.repo.find_open_position(uid, chain, token, "manual")
        reason = await self._risk(uid, chain, size, info, sec, manual=True, extra_cost=existing["cost_native"] if existing else 0.0)
        if reason is None and size > self.repo.get_balance(uid, chain) - CHAINS[chain].gas_native:
            reason = "insufficient paper balance"
        if reason:
            await self._skip(uid, chain, token, info.symbol if info else None, None, f"manual: {reason}", notify=False)
            return f"⏭ Manual buy blocked: {ui.safe(reason, 90)}"
        assert info is not None
        pos, fill, _ = await self._buy(uid, chain, token, size, info, "manual", None, reason="manual buy")
        return (f"{ui.badge(chain)} <b>🟢 MANUAL BUY</b> {ui.PAPER}\n${ui.sym(info.symbol)}<pre>Spent     {ui.amt(fill.native, chain, dp=5)}\n"
                f"Tokens    {fill.tokens:,.2f}\nFill      {ui.price(fill.price)} ({ui.pct(fill.impact_pct)} impact)\n"
                f"Fees+gas  {ui.amt(fill.fee_native + fill.gas_native, chain, dp=5)}\nBalance   {ui.amt(self.repo.get_balance(uid, chain), chain)}</pre>")

    def find_user_position(self, uid: int, token_or_sym: str, chain: str | None = None, source: str | None = None) -> dict[str, Any] | None:
        q = token_or_sym.lstrip("$").lower()
        for p in self.repo.open_positions(uid, chain):
            if source and p["source"] != source:
                continue
            if p["token"].lower() == q or (p["symbol"] or "").lower() == q:
                return p
        return None

    async def sell_position(self, uid: int, pid: int, pct: float, reason: str = "manual sell") -> str:
        pos = self.repo.get_position(pid)
        if not pos or pos["user_id"] != uid or pos["status"] != "open":
            return "Position not found or already closed."
        pct = max(0.0, min(100.0, pct))
        if pct <= 0:
            return "Percent must be between 1 and 100."
        info = await self.token_info(pos["chain"], pos["token"], max_age=0)
        tokens = pos["tokens"] if pct >= 100 else pos["tokens"] * pct / 100
        fill, pnl, closed, _ = await self._sell(uid, pos, tokens, info, reason)
        chain = pos["chain"]
        return (f"{ui.badge(chain)} <b>🔴 SELL {pct:g}%</b> {ui.PAPER} ${ui.sym(pos['symbol'])} ({pos['source']})"
                f"<pre>Received  {ui.amt(fill.native, chain, dp=5)}\nFill      {ui.price(fill.price)}\nPnL       {ui.amt(pnl, chain, True, dp=5)}\n"
                f"Status    {'closed' if closed else 'open'}\nBalance   {ui.amt(self.repo.get_balance(uid, chain), chain)}</pre>")

    async def manual_sell(self, uid: int, token_or_sym: str, pct: float) -> str:
        pos = self.find_user_position(uid, token_or_sym, source="manual") or self.find_user_position(uid, token_or_sym)
        if not pos:
            return "No open position for that token."
        return await self.sell_position(uid, pos["id"], pct, "manual sell")

    async def token_card(self, uid: int, chain: str, token: str) -> tuple[str, list[list[tuple[str, str]]]]:
        info = await self.token_info(chain, token, max_age=0)
        if not info:
            return f"{ui.badge(chain)} No DexScreener pair found for <code>{ui.short(token)}</code> on {CHAINS[chain].name}.", []
        sec = await self.security(chain, token)
        holders = self.repo.holders_of(chain, token, self.tracked_wallets(chain))
        ment = await self.mentions(info.symbol, token)
        age = ""
        if info.pair_created_at:
            hrs = (self.now() - info.pair_created_at / 1000) / 3600
            age = f"{hrs:.0f}h" if hrs < 72 else f"{hrs / 24:.0f}d"
        rug = ("✓ OK" if sec.ok else "✗ " + ", ".join(ui.safe(r, 30) for r in sec.reasons[:3])) + (f" (score {sec.score:.0f})" if sec.score is not None else "")
        text = (f"{ui.badge(chain)} <b>${ui.sym(info.symbol)}</b> — {ui.safe(info.name, 30)}\n<code>{token}</code>"
                f"<pre>Price      ${ui.price(info.price_usd)}  ({ui.price(info.price_native)} {CHAINS[chain].native})\n"
                f"MCap       {ui.usd(info.mcap_usd)}\nLiquidity  {ui.usd(info.liquidity_usd)}\nPair age   {age or '—'}  {ui.safe(info.dex)}\n"
                f"Rug-check  {rug}</pre>")
        text += "\n👀 Tracked holders: " + (", ".join(ui.safe(self.label_of(chain, w), 14) for w in holders) if holders else "none")
        text += (f"\n𝕏 {ment.count} mentions · weighted {ment.weighted}{' 🚀' if ment.spike else ''}" if ment.enabled else f"\n𝕏 {ment.note}")
        pos = [p for p in self.repo.open_positions(uid, chain) if p["token"] == token]
        presets = CHAINS[chain].manual_presets
        rows = [[(f"Buy {p:g} {CHAINS[chain].native}", f"mb:{chain}:{token}:{p:g}"[:64]) for p in presets],
                [("✏️ Custom", f"mbc:{chain}:{token}"[:64]), ("🔄 Refresh", f"card:{chain}:{token}"[:64])]]
        for p in pos:
            val = p["tokens"] * info.price_native
            text += f"\n📂 Holding ({p['source']}): {p['tokens']:,.0f} ≈ {ui.amt(val, chain)} · uPnL {ui.amt(val - p['cost_native'], chain, True)}"
            rows.append([(f"Sell 25%", f"sellp:{p['id']}:25"), ("Sell 50%", f"sellp:{p['id']}:50"), ("Sell 100%", f"sellp:{p['id']}:100")])
        return text, rows
