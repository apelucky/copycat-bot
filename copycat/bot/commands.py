"""Transport-independent command + callback handling. Telegram and the local harness both call this."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from ..chains.registry import CHAIN_ORDER, CHAINS, address_kind, find_address
from ..core import reports
from ..core.engine import Engine
from ..core.settings import active_chains
from . import ui

Buttons = list[list[tuple[str, str]]]

GLOBAL_NUM = {"copy_mult": (0.01, 2.0), "loss_switch": (1, 20), "bench_hours": (1, 720), "min_liq_usd": (0, 1e9),
              "max_slippage_pct": (0.1, 50), "max_copy_cost_pct": (0.1, 50), "health_min": (0, 1),
              "group_window_min": (1, 120), "group_entry_at": (2, 10), "group_scale_pct": (5, 100)}
CHAIN_NUM = {"max_position": (0.0001, 1e6), "daily_loss_cap": (0, 1e6), "group_size": (0.0001, 1e6)}
TOGGLES = {"groupbuy", "rug_check", "advisor", "manual_alerts", "manual_auto_sell", "pause_worst_hours"}


@dataclass
class Reply:
    text: str = ""
    buttons: Buttons = field(default_factory=list)
    photo: str | None = None
    document: str | None = None


class Commands:
    def __init__(self, engine: Engine, runner: Any = None):
        self.e = engine
        self.runner = runner

    def allowed(self, uid: int) -> bool:
        ids = self.e.cfg.allowed_user_ids
        return not ids or uid in ids

    # ----------------------------------------------------------- text
    async def handle_text(self, uid: int, chat_id: int, text: str) -> list[Reply]:
        if not self.allowed(uid):
            return [Reply("⛔ Not authorised. Ask the owner to add your Telegram user id to ALLOWED_USER_IDS.")]
        self.e.ensure_user(uid, chat_id)
        text = (text or "").strip()
        if text.startswith("/"):
            parts = text.split()
            cmd = parts[0][1:].split("@")[0].lower()
            fn = getattr(self, f"cmd_{cmd}", None)
            if not fn:
                return [Reply("Unknown command. /help")]
            return await fn(uid, parts[1:])
        pending = self.e.repo.kv_get(f"pending:{uid}")
        if pending:
            self.e.repo.kv_set(f"pending:{uid}", None)
            return await self._pending(uid, pending, text)
        ca = find_address(text)
        if ca and address_kind(ca):
            return await self._card(uid, ca)
        return [Reply("Paste a contract address for a token card, or /help.")]

    async def _pending(self, uid: int, p: dict[str, Any], text: str) -> list[Reply]:
        try:
            val = float(text.replace(",", "."))
        except ValueError:
            return [Reply("Expected a number — cancelled.")]
        if p["action"] == "mbc":
            return [Reply(await self.e.manual_buy(uid, p["chain"], p["token"], val))]
        if p["action"] == "set":
            return await self.cmd_set(uid, [p["key"], str(val)])
        return []

    async def _card(self, uid: int, token: str, chain: str | None = None) -> list[Reply]:
        if chain is None:
            chain, cands = await self.e.resolve_chain(uid, token)
            if chain is None:
                if not cands:
                    return [Reply("That chain isn't enabled. See /status.")]
                return [Reply("Which EVM chain is this token on?",
                              [[(CHAINS[c].name, f"card:{c}:{token}") for c in cands[:3]], [(CHAINS[c].name, f"card:{c}:{token}") for c in cands[3:]]])]
        text, kb = await self.e.token_card(uid, chain, token)
        return [Reply(text, kb)]

    # ----------------------------------------------------------- commands
    async def cmd_start(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply(reports.start_text(self.e, uid), ui.MAIN_KB)]

    async def cmd_help(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply(ui.HELP)]

    async def cmd_status(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply(reports.status_text(self.e, uid), ui.kb([("🔄 Refresh", "m:status"), ("🏠 Menu", "m:start")]))]

    async def cmd_chain(self, uid: int, args: list[str]) -> list[Reply]:
        s = self.e.settings(uid)
        enabled = self.e.enabled_chains()
        if args:
            c = args[0].lower()
            c = {"eth": "ethereum", "bnb": "bsc", "sol": "solana", "hood": "robinhood", "rh": "robinhood"}.get(c, c)
            if c not in CHAIN_ORDER + ["all"]:
                return [Reply("Chains: solana, base, ethereum, bsc, robinhood, all")]
            if c != "all" and c not in enabled:
                return [Reply(f"🚫 {self.e.chain_status()[c]}")]
            s["active_chain"] = c
            self.e.save_settings(uid, s)
            for ch in active_chains(s, enabled):
                if not self.e.repo.get_following(uid, ch):
                    nxt = self.e.pick_next(uid, ch)
                    if nxt:
                        self.e.follow(uid, ch, nxt["wallet"], nxt["label"])
            return await self.cmd_start(uid, [])
        lines = [f"<b>🔗 Active chain:</b> {'All' if s['active_chain'] == 'all' else CHAINS[s['active_chain']].name}"]
        for c in CHAIN_ORDER:
            acc = self.e.repo.get_account(uid, c)
            lines.append(f"{'✅' if c in enabled else '🚫'} {CHAINS[c].badge} {ui.amt(acc['balance'], c)} — {ui.safe(self.e.chain_status()[c], 110)}")
        return [Reply("\n".join(lines), ui.chain_kb(s["active_chain"], enabled))]

    def _chain_arg(self, uid: int, args: list[str]) -> str:
        if args and args[0].lower() in CHAIN_ORDER:
            return args.pop(0).lower()
        s = self.e.settings(uid)
        return s["active_chain"] if s["active_chain"] != "all" else "solana"

    async def cmd_leaderboard(self, uid: int, args: list[str], page: int = 0) -> list[Reply]:
        chain = self._chain_arg(uid, list(args))
        if not self.e.repo.leaderboard(chain):
            try:
                await self.e.refresh_leaderboard(chain)
            except Exception as ex:  # noqa: BLE001
                return [Reply(f"Leaderboard refresh failed: {ui.safe(str(ex), 80)}")]
        rows = [r for r in self.e.repo.leaderboard(chain) if not r["flagged"]]
        kb = [[("⬅️ Prev", f"lbp:{chain}:{max(0, page - 1)}"), ("Next ➡️", f"lbp:{chain}:{page + 1}")]]
        kb.append([(f"👤 Follow #{r['rank']}", f"fol:{chain}:{r['rank']}") for r in rows[page * 8: page * 8 + 3]])
        kb.append([("🔄 Refresh", f"lbr:{chain}")])
        return [Reply(reports.leaderboard_text(self.e, uid, chain, page), kb)]

    async def cmd_follow(self, uid: int, args: list[str]) -> list[Reply]:
        args = list(args)
        chain = self._chain_arg(uid, args)
        if not args:
            return [Reply("Usage: /follow &lt;rank|wallet&gt; [label]")]
        target = args[0]
        if target.isdigit():
            row = next((r for r in self.e.repo.leaderboard(chain) if r["rank"] == int(target)), None)
            if not row:
                return [Reply("No trader with that rank.")]
            self.e.follow(uid, chain, row["wallet"], row["label"])
            return [Reply(f"{ui.badge(chain)} 👑 Following <b>{ui.safe(row['label'])}</b> ({ui.short(row['wallet'])})")]
        kind = address_kind(target)
        if (kind == "evm") != (CHAINS[chain].kind == "evm") or kind is None:
            return [Reply(f"That wallet format doesn't match {CHAINS[chain].name}. Use /chain first or prefix the chain: /follow base 0x…")]
        label = " ".join(args[1:])[:20] or ui.short(target)
        self.e.follow(uid, chain, target, label)
        tracked = self.e.repo.kv_get(f"tracked:{chain}", [])
        if target not in tracked:
            tracked.append(target)
            self.e.repo.kv_set(f"tracked:{chain}", tracked)
        return [Reply(f"{ui.badge(chain)} 👑 Following <b>{ui.safe(label)}</b> ({ui.short(target)}) — manual follow, added to tracked list.")]

    async def cmd_bench(self, uid: int, args: list[str]) -> list[Reply]:
        out = ["<b>⏸ Benched traders</b>"]
        for c in CHAIN_ORDER:
            for b in self.e.repo.benched(uid, c, self.e.now()):
                left = (b["benched_until"] - self.e.now()) // 60
                out.append(f"{CHAINS[c].badge} {ui.safe(self.e.label_of(c, b['wallet']))} — {ui.safe(b['bench_reason'], 40)} · {left // 60}h{left % 60:02d}m left")
        return [Reply("\n".join(out) if len(out) > 1 else "No benched traders.")]

    async def _infos(self, uid: int) -> dict[tuple[str, str], Any]:
        infos = {}
        for c in reports.chains_for(self.e, uid):
            for p in self.e.repo.open_positions(uid, c):
                infos[(c, p["token"])] = await self.e.token_info(c, p["token"])
        return infos

    async def cmd_positions(self, uid: int, args: list[str]) -> list[Reply]:
        infos = await self._infos(uid)
        pos = [p for c in reports.chains_for(self.e, uid) for p in self.e.repo.open_positions(uid, c)]
        kb = [[(f"${ui.sym(p['symbol'], 8)} 50%", f"sellp:{p['id']}:50"), (f"${ui.sym(p['symbol'], 8)} 100%", f"sellp:{p['id']}:100")] for p in pos[:6]]
        return [Reply(reports.positions_text(self.e, uid, infos), kb)]

    async def cmd_close(self, uid: int, args: list[str]) -> list[Reply]:
        if not args:
            return [Reply("Usage: /close &lt;token|CA|#id|all&gt;")]
        if args[0] == "all":
            out = [await self.e.sell_position(uid, p["id"], 100, "close all") for p in self.e.repo.open_positions(uid)]
            return [Reply("\n".join(out) or "Nothing open.")]
        if args[0].lstrip("#").isdigit():
            return [Reply(await self.e.sell_position(uid, int(args[0].lstrip("#")), 100, "manual close"))]
        pos = self.e.find_user_position(uid, args[0])
        if not pos:
            return [Reply("No open position for that token.")]
        return [Reply(await self.e.sell_position(uid, pos["id"], 100, "manual close"))]

    async def cmd_buy(self, uid: int, args: list[str]) -> list[Reply]:
        if len(args) < 2:
            return [Reply("Usage: /buy &lt;contract address&gt; &lt;amount in native&gt; [chain]")]
        token, amount = args[0], args[1]
        try:
            size = float(amount)
        except ValueError:
            return [Reply("Amount must be a number, e.g. /buy &lt;CA&gt; 0.5")]
        if size <= 0:
            return [Reply("Amount must be positive.")]
        chain = args[2].lower() if len(args) > 2 and args[2].lower() in CHAIN_ORDER else None
        if chain is None:
            chain, cands = await self.e.resolve_chain(uid, token)
            if chain is None:
                if not cands:
                    return [Reply("Not a valid contract address (or chain disabled).")]
                return [Reply("Which chain? Add it to the command, e.g. <code>/buy CA 0.01 base</code>",
                              [[(CHAINS[c].name, f"card:{c}:{token}") for c in cands[:3]]])]
        return [Reply(await self.e.manual_buy(uid, chain, token, size))]

    async def cmd_sell(self, uid: int, args: list[str]) -> list[Reply]:
        if len(args) < 2:
            return [Reply("Usage: /sell &lt;token|CA&gt; &lt;percent&gt; (e.g. 25 / 50 / 100)")]
        try:
            pct = float(args[1].rstrip("%"))
        except ValueError:
            return [Reply("Percent must be a number.")]
        return [Reply(await self.e.manual_sell(uid, args[0], pct))]

    async def cmd_pnl(self, uid: int, args: list[str]) -> list[Reply]:
        period = args[0].lower() if args and args[0].lower() in reports.PERIODS else "7d"
        return [Reply(reports.pnl_text(self.e, uid, period),
                      ui.kb([("Today", "pnl:today"), ("7d", "pnl:7d"), ("30d", "pnl:30d")],
                            [("📄 Export CSV", "export"), ("📈 Equity chart", "chart"), ("📝 Trade log", "log")],
                            [("🖼 PnL card", f"pnlcard:{period}")]))]

    async def cmd_pnlcard(self, uid: int, args: list[str]) -> list[Reply]:
        period = args[0].lower() if args and args[0].lower() in reports.PERIODS else "7d"
        path = os.path.join(tempfile.gettempdir(), f"copycat_pnlcard_{uid}.png")
        reports.pnl_card_for(self.e, uid, period, path)
        return [Reply(f"🖼 PnL card · {period} (paper)", photo=path)]

    async def cmd_card(self, uid: int, args: list[str]) -> list[Reply]:
        pid = None
        if args and args[0].lstrip("#").isdigit():
            pid = int(args[0].lstrip("#"))
        else:
            closed = self.e.repo.closed_positions(uid, limit=1)
            pid = closed[0]["id"] if closed else None
        if pid is None:
            return [Reply("No trade yet. Usage: /card &lt;position id&gt;")]
        path = os.path.join(tempfile.gettempdir(), f"copycat_trade_{uid}_{pid}.png")
        if not reports.trade_card_for(self.e, uid, pid, path):
            return [Reply("Position not found.")]
        return [Reply(f"🖼 Trade card #{pid} (paper)", photo=path)]

    async def cmd_log(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply(reports.log_text(self.e, uid))]

    async def cmd_export(self, uid: int, args: list[str]) -> list[Reply]:
        path = os.path.join(tempfile.gettempdir(), f"copycat_fills_{uid}.csv")
        reports.export_csv(self.e, uid, path)
        return [Reply("📄 All paper fills (CSV)", document=path)]

    async def cmd_chart(self, uid: int, args: list[str]) -> list[Reply]:
        path = os.path.join(tempfile.gettempdir(), f"copycat_equity_{uid}.png")
        reports.equity_chart(self.e, uid, path)
        return [Reply("📈 Realized equity (paper)", photo=path)]

    async def cmd_hours(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply(reports.hours_text(self.e, uid))]

    async def _toggle(self, uid: int, key: str, args: list[str]) -> bool:
        s = self.e.settings(uid)
        if args and args[0].lower() in ("on", "off"):
            s[key] = args[0].lower() == "on"
        else:
            s[key] = not s[key]
        self.e.save_settings(uid, s)
        return s[key]

    async def cmd_groupbuy(self, uid: int, args: list[str]) -> list[Reply]:
        if args and args[0].lower() in ("on", "off"):
            await self._toggle(uid, "groupbuy", args)
        s = self.e.settings(uid)
        on = s["groupbuy"]
        groups = [g for g in self.e.groups.groups.values() if g.effective_count >= 2 and not g.exited]
        txt = (f"👥 Group Buy is <b>{'ON' if on else 'OFF'}</b> — alert when ≥{s['group_entry_at']} independent tracked wallets buy the same token "
               f"within {s['group_window_min']} min; paper-enter on #{s['group_entry_at']}, scale out {s['group_scale_pct']}% per extra wallet "
               f"(3rd/4th/5th) or X spike, full exit if an earliest buyer sells. Shared-funder wallets count once.")
        if groups:
            txt += "\n\nActive: " + ", ".join(f"{CHAINS[g.chain].badge} {ui.short(g.token)} ({g.effective_count})" for g in groups[:8])
        return [Reply(txt)]

    async def cmd_advisor(self, uid: int, args: list[str]) -> list[Reply]:
        if args and args[0].lower() == "now":
            if self.runner:
                await self.runner.advisor_for(uid)
            return []
        if args and args[0].lower() in ("on", "off"):
            await self._toggle(uid, "advisor", args)
        on = self.e.settings(uid)["advisor"]
        note = "" if self.e.cfg.xai_api_key else "\n⚠️ XAI_API_KEY not set — advisor will stay silent until it is."
        return [Reply(f"🤖 Grok advisor <b>{'ON' if on else 'OFF'}</b> (daily trader review + summary at the morning report; advisory only, never trades).{note}")]

    async def cmd_settings(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply(reports.settings_text(self.e, uid), reports.settings_kb(self.e, uid))]

    async def cmd_set(self, uid: int, args: list[str]) -> list[Reply]:
        if len(args) < 2:
            return [Reply("Usage: /set &lt;key&gt; &lt;value&gt;. Keys: " + ", ".join(list(GLOBAL_NUM) + list(CHAIN_NUM) + sorted(TOGGLES)))]
        key, raw = args[0], args[1]
        s = self.e.settings(uid)
        if key in TOGGLES:
            s[key] = raw.lower() in ("on", "1", "true", "yes")
        elif key in GLOBAL_NUM or key in CHAIN_NUM:
            try:
                v = float(raw)
            except ValueError:
                return [Reply("Value must be a number.")]
            lo, hi = (GLOBAL_NUM.get(key) or CHAIN_NUM[key])
            if not lo <= v <= hi:
                return [Reply(f"{key} must be between {lo:g} and {hi:g}.")]
            if key in ("loss_switch", "group_entry_at", "bench_hours", "group_window_min"):
                v = int(v)
            if key in CHAIN_NUM:
                c = s["active_chain"] if s["active_chain"] != "all" else "solana"
                s["chains"][c][key] = v
            else:
                s[key] = v
        else:
            return [Reply("Unknown setting.")]
        self.e.save_settings(uid, s)
        if key == "group_window_min":
            self.e.groups.window_s = int(s[key]) * 60
        return await self.cmd_settings(uid, [])

    async def cmd_mode(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply("🟨 Mode: <b>PAPER</b>. 🔒 LIVE is locked in this version — no private keys are stored and no real swaps are possible. "
                      "It unlocks only after a reviewed live-trading release.")]

    async def cmd_pause(self, uid: int, args: list[str]) -> list[Reply]:
        s = self.e.settings(uid)
        s["paused"] = True
        self.e.save_settings(uid, s)
        return [Reply("⏸ Paused: no new copy or group entries (sells of open positions still mirror). /resume to continue.", ui.kb([("▶️ Resume", "m:resume")]))]

    async def cmd_resume(self, uid: int, args: list[str]) -> list[Reply]:
        s = self.e.settings(uid)
        s["paused"] = False
        self.e.save_settings(uid, s)
        return [Reply("▶️ Resumed.")]

    async def cmd_morning(self, uid: int, args: list[str]) -> list[Reply]:
        return [Reply(reports.morning_report(self.e, uid))]

    @staticmethod
    def _card_kb(pid: int) -> Buttons:
        return [[("🖼 Trade card", f"tcard:{pid}")]]

    # ----------------------------------------------------------- callbacks
    async def handle_callback(self, uid: int, chat_id: int, data: str) -> list[Reply]:
        if not self.allowed(uid):
            return [Reply("⛔ Not authorised.")]
        self.e.ensure_user(uid, chat_id)
        p = data.split(":")
        k = p[0]
        menu = {"status": self.cmd_status, "lb": self.cmd_leaderboard, "pos": self.cmd_positions, "pnl": self.cmd_pnl,
                "settings": self.cmd_settings, "pause": self.cmd_pause, "resume": self.cmd_resume, "chain": self.cmd_chain,
                "gb": self.cmd_groupbuy, "help": self.cmd_help, "start": self.cmd_start}
        if k == "m" and p[1] in menu:
            return await menu[p[1]](uid, [])
        if k == "chain":
            return await self.cmd_chain(uid, [p[1]])
        if k == "undo":
            return [Reply(await self.e.undo_switch(uid, p[1]))]
        if k == "close":
            return [Reply(await self.e.sell_position(uid, int(p[1]), 100, "close button"), self._card_kb(int(p[1])))]
        if k == "sellp":
            return [Reply(await self.e.sell_position(uid, int(p[1]), float(p[2]), f"sell {p[2]}% button"), self._card_kb(int(p[1])))]
        if k == "tcard":
            return await self.cmd_card(uid, [p[1]])
        if k == "pnlcard":
            return await self.cmd_pnlcard(uid, [p[1]])
        if k == "mb":
            return [Reply(await self.e.manual_buy(uid, p[1], p[2], float(p[3])))]
        if k == "mbc":
            self.e.repo.kv_set(f"pending:{uid}", {"action": "mbc", "chain": p[1], "token": p[2]})
            return [Reply(f"Send the amount in {CHAINS[p[1]].native} to paper-buy:")]
        if k == "card":
            return await self._card(uid, p[2], p[1])
        if k == "lbp":
            return await self.cmd_leaderboard(uid, [p[1]], page=int(p[2]))
        if k == "lbr":
            await self.e.refresh_leaderboard(p[1])
            return await self.cmd_leaderboard(uid, [p[1]])
        if k == "fol":
            return await self.cmd_follow(uid, [p[1], p[2]])
        if k == "pnl":
            return await self.cmd_pnl(uid, [p[1]])
        if k == "export":
            return await self.cmd_export(uid, [])
        if k == "chart":
            return await self.cmd_chart(uid, [])
        if k == "log":
            return await self.cmd_log(uid, [])
        if k == "tog":
            await self._toggle(uid, p[1], [])
            return await self.cmd_settings(uid, [])
        if k == "set":
            self.e.repo.kv_set(f"pending:{uid}", {"action": "set", "key": p[1]})
            return [Reply(f"Send the new value for <b>{p[1]}</b>:")]
        if k == "live":
            return await self.cmd_mode(uid, [])
        return [Reply("Unknown action.")]
