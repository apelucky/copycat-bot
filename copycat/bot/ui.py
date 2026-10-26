"""Message formatting (Telegram HTML) matching the approved mockup. Pure functions → easy to test.
Token names/symbols come from chains/X and are untrusted: always pass through `safe()`."""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..chains.registry import CHAINS

PAPER = "🟨 PAPER"


def safe(s: Any, maxlen: int = 20) -> str:
    t = re.sub(r"[^\w$.\- ]", "", str(s or "?"), flags=re.UNICODE)[:maxlen] or "?"
    return html.escape(t)


def sym(s: Any, maxlen: int = 16) -> str:
    """Token symbol for display after a literal '$' (strips a leading '$' some tokens include)."""
    return safe(str(s or "?").lstrip("$"), maxlen)


def short(addr: str | None) -> str:
    if not addr:
        return "?"
    return f"{addr[:4]}...{addr[-3:]}"


def badge(chain: str) -> str:
    return CHAINS[chain].badge


def amt(x: float | None, chain: str, signed: bool = False, dp: int | None = None) -> str:
    if x is None:
        return "—"
    native = CHAINS[chain].native
    if dp is None:
        dp = 2 if native == "SOL" or abs(x) >= 1 else 4
    s = f"{x:+.{dp}f}" if signed else f"{x:.{dp}f}"
    return f"{s} {native}"


def pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}%" if signed else f"{x:.1f}%"


def usd(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:.0f}"


def price(x: float) -> str:
    if x == 0:
        return "0"
    if x >= 0.01:
        return f"{x:.4f}"
    return f"{x:.4g}"


def color_pnl(x: float, chain: str) -> str:
    return ("🟢 " if x > 0 else "🔴 ") + amt(x, chain, signed=True)


def kb(*rows: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    return [list(r) for r in rows]


MAIN_KB = kb([("📊 Status", "m:status"), ("🏆 Leaderboard", "m:lb"), ("📂 Positions", "m:pos")],
             [("💰 PnL", "m:pnl"), ("⚙️ Settings", "m:settings"), ("⏸ Pause", "m:pause")],
             [("🔗 Chain", "m:chain"), ("👥 Group Buy", "m:gb"), ("❓ Help", "m:help")])


def chain_kb(current: str, enabled: list[str]) -> list[list[tuple[str, str]]]:
    row1, row2 = [], []
    for i, c in enumerate(["solana", "base", "ethereum", "bsc", "robinhood"]):
        mark = "✅ " if current == c else ("🚫 " if c not in enabled else "")
        (row1 if i < 3 else row2).append((f"{mark}{CHAINS[c].name.split()[0]}", f"chain:{c}"))
    row2.append((("✅ " if current == "all" else "") + "All", "chain:all"))
    return [row1, row2]


def fmt_ts(ts: int, tz: ZoneInfo, fmt: str = "%H:%M") -> str:
    return datetime.fromtimestamp(ts, tz).strftime(fmt)


HELP = """<b>🐱 COPYCAT COPY-TRADER</b> — PAPER mode (simulated money)

<b>Main</b>
/start – menu & balances
/status – bot status, data sources
/chain [solana|base|ethereum|bsc|robinhood|all] – active chain
/leaderboard – ranked traders
/follow &lt;rank|wallet&gt; – follow a trader
/bench – benched traders
/positions – open positions
/close &lt;token|CA|all&gt; – close a position
/buy &lt;CA&gt; &lt;amount&gt; – manual paper buy
/sell &lt;token|CA&gt; &lt;percent&gt; – manual paper sell
/pnl [today|7d|30d] – PnL report
/pnlcard [today|7d|30d] – shareable PnL image
/card [id] – trade card image
/log – recent fills & skips
/export – CSV of all fills
/chart – equity chart
/hours – time-of-day stats (WAT)
/groupbuy [on|off] – Group Buy mode
/advisor [on|off] – optional Grok advisor
/settings – settings
/mode – paper/live (live locked)
/pause, /resume
/help

Paste any contract address to get a token card."""


def safe_block(text: str, maxlen: int = 1500) -> str:
    """Escape free text (e.g. advisor output) for Telegram HTML."""
    return html.escape(str(text)[:maxlen])
