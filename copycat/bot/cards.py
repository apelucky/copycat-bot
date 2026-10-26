"""Shareable PNG cards (trade card + PnL card) rendered with Pillow. Fonts come from matplotlib's bundled
DejaVu set so rendering works on Railway without system fonts. Token text is sanitized before drawing."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
from PIL import Image, ImageDraw, ImageFont

from ..chains.registry import CHAINS

FONT_DIR = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
W, H = 1080, 1350
BG_TOP, BG_BOT = (16, 24, 36), (8, 12, 18)
GREEN, RED, YELLOW, GREY, WHITE, BLUE = (79, 212, 122), (255, 107, 107), (245, 197, 24), (138, 155, 173), (233, 237, 241), (106, 179, 243)
CHAIN_COLORS = {"solana": (153, 69, 255), "base": (0, 82, 255), "ethereum": (98, 126, 234), "bsc": (240, 185, 11), "robinhood": (204, 255, 0)}


def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSansMono" if mono else "DejaVuSans"
    if bold:
        name += "-Bold"
    return ImageFont.truetype(str(FONT_DIR / f"{name}.ttf"), size)


def clean(s: Any, n: int = 14) -> str:
    return re.sub(r"[^A-Za-z0-9$._\- ]", "", str(s or "?").lstrip("$"))[:n] or "?"


def _base(h: int = H) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, h), BG_BOT)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (W, y)], fill=tuple(int(BG_TOP[i] * (1 - t) + BG_BOT[i] * t) for i in range(3)))
    return img, d


def _header(d: ImageDraw.ImageDraw, chain: str | None, subtitle: str) -> None:
    d.ellipse([60, 60, 160, 160], fill=(226, 120, 60))
    d.text((110, 110), "CC", font=font(40, True), fill=WHITE, anchor="mm")
    d.text((190, 72), "COPYCAT COPY-TRADER", font=font(46, True), fill=WHITE)
    d.text((190, 130), subtitle, font=font(30), fill=GREY)
    d.rounded_rectangle([W - 230, 78, W - 60, 138], 12, fill=YELLOW)
    d.text((W - 145, 108), "PAPER", font=font(32, True), fill=(20, 20, 20), anchor="mm")
    if chain:
        c = CHAIN_COLORS.get(chain, BLUE)
        d.rounded_rectangle([60, 200, 60 + 40 + 24 * len(CHAINS[chain].name), 256], 28, fill=c)
        d.text((80, 228), CHAINS[chain].name.upper(), font=font(28, True), fill=(10, 10, 10) if chain in ("bsc", "robinhood") else WHITE, anchor="lm")


def _footer(d: ImageDraw.ImageDraw, h: int, note: str) -> None:
    d.line([(60, h - 120), (W - 60, h - 120)], fill=(40, 55, 70), width=2)
    d.text((60, h - 90), note, font=font(26), fill=GREY)
    d.text((W - 60, h - 90), "simulated funds", font=font(26), fill=GREY, anchor="ra")


def trade_card(t: dict[str, Any], path: str, tz: ZoneInfo, note: str = "") -> str:
    """t keys: chain, symbol, source, trader, cost, proceeds, pnl, entry, exit, slippage, fees, opened_at, closed_at, status."""
    chain = t["chain"]
    native = CHAINS[chain].native
    img, d = _base()
    _header(d, chain, "Trade card")
    pnl, cost = t["pnl"], max(t["cost"], 1e-18)
    pct = pnl / cost * 100
    col = GREEN if pnl > 0 else RED
    sym, size = f"${clean(t['symbol'])}", 110
    while size > 50 and d.textlength(sym, font=font(size, True)) > W - 120:
        size -= 6
    d.text((60, 300), sym, font=font(size, True), fill=WHITE)
    src = {"copy": f"copied from {clean(t.get('trader'), 16)}", "group": "group buy", "manual": "manual trade"}.get(t["source"], t["source"])
    d.text((64, 440), src, font=font(36), fill=BLUE)
    d.text((60, 520), f"{pct:+.1f}%", font=font(190, True), fill=col)
    d.text((64, 760), f"{pnl:+.4f} {native}", font=font(56, True), fill=col)
    rows = [("Entry", f"{t['entry']:.6g} {native}"), ("Exit", f"{t['exit']:.6g} {native}" if t.get("exit") else "open"),
            ("Size", f"{t['cost']:.4f} {native}"), ("Fees + gas", f"{t.get('fees', 0):.5f} {native}"),
            ("Slippage vs leader", f"{t['slippage']:+.2f}%" if t.get("slippage") is not None else "n/a"),
            ("Held", _held(t.get("opened_at"), t.get("closed_at")))]
    y = 860
    for k, v in rows:
        d.text((64, y), k, font=font(34), fill=GREY)
        d.text((W - 64, y), v, font=font(34, mono=True), fill=WHITE, anchor="ra")
        y += 58
    when = datetime.fromtimestamp(t.get("closed_at") or t.get("opened_at") or 0, tz).strftime("%d %b %Y %H:%M WAT")
    _footer(d, H, note or when)
    img.save(path, optimize=True)
    return path


def _held(a: int | None, b: int | None) -> str:
    if not a or not b:
        return "—"
    m = max(0, b - a) // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def pnl_card(period_label: str, chains: list[dict[str, Any]], path: str, note: str = "") -> str:
    """chains: [{chain, net, pct, n, win_rate, best_sym, best, worst_sym, worst, avg_slip, top_trader}]"""
    h = 220 + 330 * max(1, len(chains)) + 150
    img, d = _base(h)
    _header(d, None, f"PnL report · {period_label}")
    y = 220
    for c in chains:
        chain, native = c["chain"], CHAINS[c["chain"]].native
        col = GREEN if c["net"] > 0 else (RED if c["net"] < 0 else WHITE)
        d.rounded_rectangle([50, y, W - 50, y + 300], 24, fill=(20, 32, 46))
        d.rounded_rectangle([50, y, 62, y + 300], 6, fill=CHAIN_COLORS.get(chain, BLUE))
        d.text((90, y + 24), CHAINS[chain].name, font=font(36, True), fill=WHITE)
        d.text((W - 90, y + 20), f"{c['net']:+.4f} {native}", font=font(54, True), fill=col, anchor="ra")
        d.text((W - 90, y + 88), f"{c['pct']:+.1f}%", font=font(34, True), fill=col, anchor="ra")
        stats = [f"Trades {c['n']}   Win {c['win_rate']:.0%}",
                 f"Best  ${clean(c.get('best_sym'))} {c.get('best', 0):+.4f}" if c.get("best_sym") else "Best  —",
                 f"Worst ${clean(c.get('worst_sym'))} {c.get('worst', 0):+.4f}" if c.get("worst_sym") else "Worst —",
                 f"Avg slippage vs leader {c['avg_slip']:+.2f}%" if c.get("avg_slip") is not None else "Avg slippage vs leader n/a"]
        for i, s in enumerate(stats):
            d.text((90, y + 92 + i * 48), s, font=font(30, mono=True), fill=WHITE if i == 0 else GREY)
        y += 330
    _footer(d, h, note or "Copycat paper trading")
    img.save(path, optimize=True)
    return path
