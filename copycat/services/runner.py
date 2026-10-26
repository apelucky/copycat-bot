"""Background loops: wallet polling per chain, leaderboard refresh, 9 AM morning report, advisor."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from ..bot import ui
from ..core import reports, stats
from ..core.advisor import review
from ..core.engine import Engine

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, engine: Engine):
        self.e = engine
        self.tasks: list[asyncio.Task] = []
        self.stop = asyncio.Event()

    async def poll_chain_once(self, chain: str) -> int:
        e = self.e
        wallets = e.watch_list(chain)
        state = e.repo.kv_get(f"cursor:{chain}", {})
        events = await e.adapters[chain].poll(wallets, state)
        e.repo.kv_set(f"cursor:{chain}", state)
        for ev in events:
            await e.on_event(ev)
        hb = e.repo.kv_get("heartbeat", {})
        hb[chain] = {"ts": int(time.time()), "wallets": len(wallets), "events": hb.get(chain, {}).get("events", 0) + len(events)}
        e.repo.kv_set("heartbeat", hb)
        return len(events)

    async def poll_loop(self, chain: str) -> None:
        backoff = self.e.cfg.poll_seconds
        while not self.stop.is_set():
            try:
                await self.poll_chain_once(chain)
                backoff = self.e.cfg.poll_seconds
            except Exception as ex:  # noqa: BLE001
                backoff = min(backoff * 2, 300)
                log.warning("poll %s failed (%s); retry in %ss", chain, str(ex)[:120], backoff)
            await self._sleep(backoff)

    async def leaderboard_loop(self) -> None:
        while not self.stop.is_set():
            for c in self.e.enabled_chains():
                try:
                    await self.e.refresh_leaderboard(c)
                except Exception as ex:  # noqa: BLE001
                    log.warning("leaderboard %s failed: %s", c, ex)
            await self._sleep(self.e.cfg.leaderboard_refresh_minutes * 60)

    async def daily_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.daily_tick()
            except Exception as ex:  # noqa: BLE001
                log.warning("daily tick failed: %s", ex)
            await self._sleep(30)

    async def daily_tick(self, force: bool = False) -> None:
        e = self.e
        now = datetime.fromtimestamp(e.now(), e.cfg.tz)
        day = now.strftime("%Y-%m-%d")
        if not force and (now.hour != e.cfg.morning_report_hour or e.repo.kv_get("morning_sent") == day):
            return
        e.repo.kv_set("morning_sent", day)
        for u in e.repo.list_users():
            uid = u["user_id"]
            await e.notifier.send(uid, reports.morning_report(e, uid), ui.kb([("💰 PnL", "pnl:today"), ("🏆 Leaderboard", "m:lb")]))
            if e.settings(uid)["advisor"] and e.cfg.xai_api_key:
                await self.advisor_for(uid)

    async def advisor_for(self, uid: int) -> str:
        e = self.e
        if not e.cfg.xai_api_key:
            return "Grok advisor unavailable: set XAI_API_KEY."
        payload: dict[str, Any] = {"mode": "paper", "chains": {}}
        for c in reports.chains_for(e, uid):
            closed = e.repo.closed_positions(uid, e.now() - 86400, c)
            fills = e.repo.fills(uid, e.now() - 86400, c)
            labels = {r["wallet"]: r["label"] for r in e.repo.leaderboard(c)}
            payload["chains"][c] = {"summary": {k: v for k, v in stats.summarize(closed, fills).items() if k not in ("best", "worst")},
                                    "per_trader": stats.per_trader(closed, fills, labels),
                                    "following": (e.repo.get_following(uid, c) or {}).get("label")}
        try:
            text = await review(e.adapters[next(iter(e.adapters))].http, e.cfg.xai_api_key, e.cfg.xai_model, payload)  # type: ignore[attr-defined]
        except Exception as ex:  # noqa: BLE001
            text = f"Grok advisor failed: {str(ex)[:100]}"
        msg = "<b>🤖 Grok advisor (advisory only, never trades)</b>\n" + ui.safe_block(text)
        await e.notifier.send(uid, msg)
        return msg

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    def start(self) -> None:
        for c in self.e.enabled_chains():
            self.tasks.append(asyncio.create_task(self.poll_loop(c), name=f"poll-{c}"))
        self.tasks.append(asyncio.create_task(self.leaderboard_loop(), name="leaderboard"))
        self.tasks.append(asyncio.create_task(self.daily_loop(), name="daily"))

    async def shutdown(self) -> None:
        self.stop.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
