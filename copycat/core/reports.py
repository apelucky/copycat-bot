"""Text reports for Telegram screens (status, leaderboard, positions, PnL, log, settings, morning report)."""
from __future__ import annotations

import csv
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..bot import ui
from ..chains.registry import CHAIN_ORDER, CHAINS
from . import ranking, stats
from .settings import active_chains

PERIODS = {"today": None, "7d": 7, "30d": 30}


def chains_for(engine: Any, uid: int) -> list[str]:
    return active_chains(engine.settings(uid), engine.enabled_chains()) or ["solana"]


def start_text(engine: Any, uid: int) -> str:
    s = engine.settings(uid)
    out = [f"<b>🐱 COPYCAT COPY-TRADER</b> {ui.PAPER}", "Paper trading mode — simulated funds, no real swaps.",
           f"Active chain: <b>{'All' if s['active_chain'] == 'all' else CHAINS[s['active_chain']].name}</b>"]
    for c in chains_for(engine, uid):
        acc = engine.repo.get_account(uid, c)
        today = engine.realized_since(uid, c, engine.day_start())
        pos = engine.repo.open_positions(uid, c)
        expo = sum(p["cost_native"] for p in pos)
        cap = s["chains"][c]["daily_loss_cap"]
        f = engine.repo.get_following(uid, c)
        lead = "none (run /leaderboard or /follow)"
        if f:
            st = engine.repo.trader_state(uid, c, f["wallet"])
            row = next((r for r in engine.repo.leaderboard(c) if r["wallet"] == f["wallet"]), None)
            lead = f"{ui.safe(f['label'])}  {ui.short(f['wallet'])}"
            if row:
                lead += f"\n7d PnL {row['pnl']:+.0f}  Win {row['wins'] / max(row['trades'], 1):.0%}  Trades {row['trades']}"
            lead += f"\nLosses-in-row {st['consec_losses']}/{s['loss_switch']}"
        out.append(f"\n{ui.badge(c)}<pre>Balance   {ui.amt(acc['balance'], c)}  (start {acc['start_balance']:g})\n"
                   f"Today     {ui.amt(today, c, True)}\nOpen pos. {len(pos)}  |  Exposure {ui.amt(expo, c)}\n"
                   f"Loss cap  {max(0, -today):.4g} / {cap:g} {CHAINS[c].native} used</pre>👑 <b>Leader</b><pre>{lead}</pre>")
    state = "⏸ Paused" if s["paused"] else "● Copying (paper)"
    out.append(f"Status: {state} · Group Buy {'ON' if s['groupbuy'] else 'OFF'}")
    return "\n".join(out)


def status_text(engine: Any, uid: int) -> str:
    s = engine.settings(uid)
    lines = [f"<b>📊 Status</b> {ui.PAPER}", f"Mode: PAPER (live locked) · {'⏸ paused' if s['paused'] else '▶️ running'}", "", "<b>Data sources</b>"]
    for c, msg in engine.chain_status().items():
        on = c in engine.enabled_chains()
        lines.append(f"{'✅' if on else '🚫'} {ui.safe(msg, 120)}")
    lines.append(f"{'✅' if engine.x and engine.x.enabled else '🚫'} X mentions {'(X_BEARER_TOKEN set)' if engine.x and engine.x.enabled else 'disabled — no X_BEARER_TOKEN'}")
    lines.append(f"{'✅' if engine.cfg.xai_api_key else '🚫'} Grok advisor {'available' if engine.cfg.xai_api_key else 'unavailable — no XAI_API_KEY'} (setting {'ON' if s['advisor'] else 'OFF'})")
    hb = engine.repo.kv_get("heartbeat", {})
    for c in CHAIN_ORDER:
        if c in hb:
            lines.append(f"{ui.badge(c)} last poll {int(time.time() - hb[c]['ts'])}s ago · {hb[c]['wallets']} wallets · {hb[c].get('events', 0)} events")
    return "\n".join(lines)


def leaderboard_text(engine: Any, uid: int, chain: str, page: int = 0, per: int = 8) -> str:
    rows = engine.repo.leaderboard(chain)
    if not rows:
        return f"{ui.badge(chain)} No leaderboard yet. Sources: {'Kolscan/GMGN' if chain == 'solana' else 'GMGN (often blocked) + seed list'}. Add wallets with /follow &lt;wallet&gt;."
    f = engine.repo.get_following(uid, chain)
    ok = [r for r in rows if not r["flagged"]]
    bad = [r for r in rows if r["flagged"]]
    srcs = sorted({r["source"] for r in rows})
    body = [" #  Trader        PnL   Win  Trd"]
    for r in ok[page * per:(page + 1) * per]:
        tag = ""
        if f and f["wallet"] == r["wallet"]:
            tag = " ◀ FOLLOWING"
        elif engine.is_benched(uid, chain, r["wallet"]):
            tag = " ⏸ BENCHED"
        body.append(f"{r['rank']:>2}  {ui.safe(r['label'], 12):<12} {r['pnl']:>+6.0f} {r['wins'] / max(r['trades'], 1):>4.0%} {r['trades']:>4}{tag}")
    for r in bad[:3]:
        body.append(f" –  {ui.safe(r['label'], 12):<12} {r['pnl']:>+6.0f} {r['wins'] / max(r['trades'], 1):>4.0%} {r['trades']:>4} ⚠ {ui.safe(r['flagged'], 18)}")
    return (f"{ui.badge(chain)} <b>🏆 Weekly leaderboard</b>\nSource: {', '.join(srcs)} · 7d · {len(bad)} filtered (bot/bait/thin)"
            f"<pre>" + "\n".join(body) + "</pre>Rank score = PnL · win rate · trade count (min 30 trades).")


def positions_text(engine: Any, uid: int, infos: dict[tuple[str, str], Any]) -> str:
    pos = [p for c in chains_for(engine, uid) for p in engine.repo.open_positions(uid, c)]
    if not pos:
        return "📂 No open positions."
    out = [f"<b>📂 Open positions</b> {ui.PAPER}"]
    for p in pos:
        info = infos.get((p["chain"], p["token"]))
        val = p["tokens"] * info.price_native if info else None
        upnl = (val - p["cost_native"]) if val is not None else None
        who = p["source"] if p["source"] != "copy" else ui.safe(engine.label_of(p["chain"], p["trader"]), 12)
        out.append(f"{ui.badge(p['chain'])} <b>${ui.sym(p['symbol'])}</b> #{p['id']} · {who}\n"
                   f"   cost {ui.amt(p['cost_native'], p['chain'])} · value {ui.amt(val, p['chain'])} · uPnL {ui.amt(upnl, p['chain'], True)}")
    return "\n".join(out)


def pnl_text(engine: Any, uid: int, period: str = "7d") -> str:
    days = PERIODS.get(period, 7)
    since = engine.day_start() if days is None else engine.now() - days * 86400
    out = [f"<b>💰 PnL report · {'today' if days is None else f'last {days} days'}</b> {ui.PAPER}"]
    for c in chains_for(engine, uid):
        closed = engine.repo.closed_positions(uid, since, c)
        fills = engine.repo.fills(uid, since, c)
        sm = stats.summarize(closed, fills)
        skips = engine.repo.skips(uid, since)
        skips = [k for k in skips if k["chain"] == c]
        reasons = defaultdict(int)
        for k in skips:
            reasons[k["reason"].split(":")[0].split(" ")[0]] += 1
        sw = [x for x in engine.repo.kv_get(f"switches:{uid}", []) if x["ts"] >= since and x["chain"] == c]
        best = f"{sm['best']['realized']:+.4g}  ${ui.sym(sm['best']['symbol'], 10)}" if sm["best"] else "—"
        worst = f"{sm['worst']['realized']:+.4g}  ${ui.sym(sm['worst']['symbol'], 10)}" if sm["worst"] else "—"
        start = engine.repo.get_account(uid, c)["start_balance"]
        out.append(f"\n{ui.badge(c)}<pre>Net PnL       {ui.amt(sm['net'], c, True)} ({sm['net'] / start * 100:+.1f}%)\n"
                   f"Closed        {sm['n']}  (W {sm['wins']} / L {sm['losses']})\nWin rate      {sm['win_rate']:.1%}\n"
                   f"Avg win       {ui.amt(sm['avg_win'], c, True)}\nAvg loss      {ui.amt(sm['avg_loss'], c, True)}\n"
                   f"Best          {best}\nWorst         {worst}\nAvg slippage  {ui.pct(sm['avg_slip'])} vs leader\n"
                   f"Fees+gas      {ui.amt(sm['fees'], c, dp=5)}\n"
                   f"Skipped       {len(skips)}" + (f" ({', '.join(f'{k} {v}' for k, v in list(reasons.items())[:3])})" if reasons else "") +
                   f"\nSwitches      {len(sw)}</pre>")
        labels = {r["wallet"]: r["label"] for r in engine.repo.leaderboard(c)}
        pt = stats.per_trader(closed, fills, labels)
        if pt:
            out.append("<b>Per trader</b><pre>Trader        Cp  Win     PnL  Slip\n" + "\n".join(
                f"{ui.safe(r['label'], 12):<12} {r['n']:>3} {r['win_rate']:>4.0%} {r['pnl']:>+7.3f} {('%.1f%%' % r['slip']) if r['slip'] is not None else '  —'}"
                for r in pt[:8]) + "</pre>")
    return "\n".join(out)


def log_text(engine: Any, uid: int, n: int = 12) -> str:
    fills = engine.repo.fills(uid, engine.now() - 7 * 86400)[-n:]
    skips = engine.repo.skips(uid, engine.now() - 7 * 86400)[:6]
    out = [f"<b>📝 Trade log</b> {ui.PAPER}"]
    for f in reversed(fills):
        slip = f" slip {f['slippage_pct']:+.1f}%" if f["slippage_pct"] is not None else ""
        pnl = f" pnl {f['pnl_native']:+.4g}" if f["pnl_native"] is not None else ""
        out.append(f"{ui.fmt_ts(f['ts'], engine.cfg.tz, '%d %H:%M')} {CHAINS[f['chain']].badge} {f['side'].upper()} ${ui.sym(f['symbol'], 10)} "
                   f"[{f['source']}] {f['native']:.4g}{slip}{pnl}")
    if skips:
        out.append("\n<b>Skipped</b>")
        for k in skips:
            out.append(f"{ui.fmt_ts(k['ts'], engine.cfg.tz, '%d %H:%M')} {CHAINS[k['chain']].badge} ${ui.sym(k['symbol'], 10)} — {ui.safe(k['reason'], 60)}")
    return "\n".join(out) if len(out) > 1 else "📝 No fills yet."


def settings_text(engine: Any, uid: int) -> str:
    s = engine.settings(uid)
    c = s["active_chain"] if s["active_chain"] != "all" else "solana"
    cs = s["chains"][c]
    n = CHAINS[c].native
    rows = [("Mode", "🟨 PAPER · 🔒 LIVE locked"), ("Active chain", "All" if s["active_chain"] == "all" else CHAINS[c].name),
            ("Copy size", f"{s['copy_mult']}x leader"), (f"Max position ({n})", f"{cs['max_position']:g}"),
            ("Switch trader after", f"{s['loss_switch']} losses in a row"), ("Bench dropped trader", f"{s['bench_hours']} h"),
            (f"Daily loss cap ({n})", f"{cs['daily_loss_cap']:g} (auto-pause)"), ("Min liquidity", f"${s['min_liq_usd']:,.0f}"),
            ("Rug-check skip", "ON" if s["rug_check"] else "OFF"), ("Max slippage", f"{s['max_slippage_pct']:g}%"),
            ("Max copy cost vs leader", f"{s['max_copy_cost_pct']:g}%"), ("Health floor", f"{s['health_min']:g}"),
            ("Group Buy", f"{'ON' if s['groupbuy'] else 'OFF'} · {s['group_window_min']}m · enter @{s['group_entry_at']} · {cs['group_size']:g} {n}"),
            ("Manual: alerts / auto-sell", f"{'ON' if s['manual_alerts'] else 'OFF'} / {'ON' if s['manual_auto_sell'] else 'OFF'}"),
            ("Pause worst hours", "ON" if s["pause_worst_hours"] else "OFF"),
            ("Grok advisor (optional)", "ON" if s["advisor"] else "OFF")]
    w = max(len(k) for k, _ in rows)
    return ("<b>⚙️ Settings</b> " + ui.PAPER + "<pre>" + "\n".join(f"{k:<{w}}  {v}" for k, v in rows) + "</pre>"
            "Change: <code>/set key value</code> e.g. <code>/set copy_mult 0.3</code>, <code>/set max_position 0.4</code> (active chain).\n"
            "Live trading is locked; decisions are rule-based — Grok only reviews/summarizes.")


def settings_kb(engine: Any, uid: int) -> list[list[tuple[str, str]]]:
    s = engine.settings(uid)
    return ui.kb([("📏 Copy size", "set:copy_mult"), ("🛑 Loss cap", "set:daily_loss_cap")],
                 [("💧 Min liquidity", "set:min_liq_usd"), ("🔁 Switch rules", "set:loss_switch")],
                 [(f"👥 Group Buy: {'ON' if s['groupbuy'] else 'OFF'}", "tog:groupbuy"), (f"🛡 Rug-check: {'ON' if s['rug_check'] else 'OFF'}", "tog:rug_check")],
                 [(f"✋ Manual auto-sell: {'ON' if s['manual_auto_sell'] else 'OFF'}", "tog:manual_auto_sell"), (f"🕐 Worst hours: {'ON' if s['pause_worst_hours'] else 'OFF'}", "tog:pause_worst_hours")],
                 [(f"🤖 Grok advisor: {'ON' if s['advisor'] else 'OFF'}", "tog:advisor"), ("🔒 Live (locked)", "live")],
                 [("🔗 Chain", "m:chain")])


def hours_text(engine: Any, uid: int) -> str:
    closed = engine.repo.closed_positions(uid, engine.now() - 30 * 86400)
    bh = stats.by_hour(closed, engine.cfg.tz)
    if not bh:
        return "🕐 No closed trades yet for time-of-day stats."
    worst = stats.worst_hours(closed, engine.cfg.tz)
    lines = [f"{h:02d}:00  n={d['n']:<3} win {d['wins'] / d['n']:.0%}  pnl {d['pnl']:+.4f}{'  ⚠ worst' if h in worst else ''}" for h, d in sorted(bh.items())]
    return f"<b>🕐 Time-of-day ({engine.cfg.tz_name}) · 30d</b><pre>" + "\n".join(lines) + "</pre>Pause worst hours: " + ("ON" if engine.settings(uid)["pause_worst_hours"] else "OFF (toggle in /settings)")


def morning_report(engine: Any, uid: int) -> str:
    y0, y1 = engine.day_start(1), engine.day_start(0)
    out = [f"<b>☀️ Morning report</b> {ui.PAPER} · {datetime.fromtimestamp(y0, engine.cfg.tz).strftime('%a %d %b')} (WAT)"]
    for c in chains_for(engine, uid):
        closed = [p for p in engine.repo.closed_positions(uid, y0, c) if p["closed_at"] < y1]
        net = sum(p["realized"] for p in closed)
        best = max(closed, key=lambda p: p["realized"], default=None)
        worst = min(closed, key=lambda p: p["realized"], default=None)
        f = engine.repo.get_following(uid, c)
        sw = [x for x in engine.repo.kv_get(f"switches:{uid}", []) if y0 <= x["ts"] < y1 and x["chain"] == c]
        out.append(f"\n{ui.badge(c)} Yesterday {ui.amt(net, c, True)} · {len(closed)} closed · balance {ui.amt(engine.repo.get_balance(uid, c), c)}")
        if best:
            out.append(f"  Best ${ui.sym(best['symbol'], 10)} {best['realized']:+.4g} · Worst ${ui.sym(worst['symbol'], 10)} {worst['realized']:+.4g}")
        out.append(f"  Following: {ui.safe(f['label']) if f else 'none'}" + (f" · {len(sw)} switch(es)" if sw else ""))
        buys = engine.repo.leader_events(c, engine.now() - 86400, "buy")
        tracked = set(engine.tracked_wallets(c))
        by_tok: dict[str, set[str]] = defaultdict(set)
        sym: dict[str, str] = {}
        for b in buys:
            if b["wallet"] in tracked:
                by_tok[b["token"]].add(b["wallet"])
                sym[b["token"]] = b["symbol"] or ui.short(b["token"])
        hot = sorted(by_tok.items(), key=lambda kv: -len(kv[1]))[:5]
        if hot:
            out.append("  Traders buying: " + ", ".join(f"${ui.sym(sym[t], 10)} ({len(w)})" for t, w in hot))
    return "\n".join(out)


def export_csv(engine: Any, uid: int, path: str) -> str:
    rows = engine.repo.fills(uid, 0)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        cols = ["ts", "chain", "side", "source", "symbol", "token", "trader", "tokens", "native", "price", "leader_price",
                "slippage_pct", "impact_pct", "fee_native", "gas_native", "pnl_native", "reason", "leader_sig"]
        w.writerow(["time_wat"] + cols)
        for r in rows:
            w.writerow([ui.fmt_ts(r["ts"], engine.cfg.tz, "%Y-%m-%d %H:%M:%S")] + [r.get(c) for c in cols])
    return path


def equity_chart(engine: Any, uid: int, path: str) -> str | None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chains = chains_for(engine, uid)
    fig, axes = plt.subplots(len(chains), 1, figsize=(7, 2.6 * len(chains)), squeeze=False)
    any_data = False
    for ax, c in zip(axes[:, 0], chains):
        start = engine.repo.get_account(uid, c)["start_balance"]
        xs, ys, eq = [], [], start
        for f in engine.repo.fills(uid, 0, c):
            if f["side"] == "sell":
                eq += f["pnl_native"] or 0
                xs.append(datetime.fromtimestamp(f["ts"], engine.cfg.tz))
                ys.append(eq)
        if xs:
            any_data = True
        ax.plot(xs or [datetime.now(engine.cfg.tz)], ys or [start], color="#4fd47a", lw=2)
        ax.axhline(start, color="#888", lw=0.8, ls="--")
        ax.set_title(f"{CHAINS[c].name} realized equity ({CHAINS[c].native}) — PAPER", fontsize=10)
        ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path if any_data else path


def trade_card_for(engine: Any, uid: int, pid: int, path: str) -> str | None:
    from ..bot.cards import trade_card

    p = engine.repo.get_position(pid)
    if not p or p["user_id"] != uid:
        return None
    fills = [f for f in engine.repo.fills(uid, 0, p["chain"]) if f["position_id"] == pid]
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    tok_b = sum(f["tokens"] for f in buys) or 1
    tok_s = sum(f["tokens"] for f in sells)
    slips = [f["slippage_pct"] for f in buys if f["slippage_pct"] is not None]
    t = {"chain": p["chain"], "symbol": p["symbol"], "source": p["source"],
         "trader": engine.label_of(p["chain"], p["trader"]) if p["trader"] else None,
         "cost": p["initial_cost"], "pnl": p["realized"],
         "entry": sum(f["price"] * f["tokens"] for f in buys) / tok_b,
         "exit": (sum(f["price"] * f["tokens"] for f in sells) / tok_s) if tok_s else None,
         "slippage": sum(slips) / len(slips) if slips else None, "fees": p["fees"],
         "opened_at": p["opened_at"], "closed_at": p["closed_at"]}
    if p["status"] == "open":
        t["note"] = "position still open (realized part only)"
    return trade_card(t, path, engine.cfg.tz, t.get("note", ""))


def pnl_card_for(engine: Any, uid: int, period: str, path: str) -> str:
    from ..bot.cards import pnl_card

    days = PERIODS.get(period, 7)
    since = engine.day_start() if days is None else engine.now() - days * 86400
    rows = []
    for c in chains_for(engine, uid):
        closed = engine.repo.closed_positions(uid, since, c)
        sm = stats.summarize(closed, engine.repo.fills(uid, since, c))
        start = engine.repo.get_account(uid, c)["start_balance"]
        rows.append({"chain": c, "net": sm["net"], "pct": sm["net"] / start * 100, "n": sm["n"], "win_rate": sm["win_rate"],
                     "best_sym": sm["best"]["symbol"] if sm["best"] else None, "best": sm["best"]["realized"] if sm["best"] else 0,
                     "worst_sym": sm["worst"]["symbol"] if sm["worst"] else None, "worst": sm["worst"]["realized"] if sm["worst"] else 0,
                     "avg_slip": sm["avg_slip"]})
    label = "today" if days is None else f"last {days} days"
    return pnl_card(label, rows, path, datetime.fromtimestamp(engine.now(), engine.cfg.tz).strftime("%d %b %Y %H:%M WAT"))
