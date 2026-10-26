"""Leaderboard sources. Kolscan (Solana) works via its public page payload; GMGN is Cloudflare-protected
and usually returns 403 from servers, so it is attempted and falls back gracefully. Seed file always works."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..chains.base import LeaderboardEntry
from .http import Http

log = logging.getLogger(__name__)


def parse_kolscan_html(html: str, timeframe: int = 7) -> list[LeaderboardEntry]:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    txt = "".join(json.loads('"' + c + '"') for c in chunks) if chunks else html
    i = txt.find('"initLeaderboard":')
    if i < 0:
        return []
    j = txt.find("[", i)
    arr, _ = json.JSONDecoder().raw_decode(txt[j:])
    out = []
    for a in arr:
        if a.get("timeframe") != timeframe:
            continue
        wins, losses = int(a.get("wins") or 0), int(a.get("losses") or 0)
        out.append(LeaderboardEntry(
            chain="solana", wallet=a["wallet_address"], label=str(a.get("name") or a["wallet_address"][:6])[:24],
            pnl_native=float(a.get("profit") or 0), wins=wins, losses=losses, trades=wins + losses,
            source="kolscan", extra={"twitter": a.get("twitter")},
        ))
    return out


async def kolscan(http: Http, timeframe: int = 7) -> list[LeaderboardEntry]:
    html = await http.get(f"https://kolscan.io/leaderboard?timeframe={timeframe}", expect_json=False)
    return parse_kolscan_html(html, timeframe)


def parse_gmgn(chain: str, d: dict[str, Any]) -> list[LeaderboardEntry]:
    rows = ((d or {}).get("data") or {}).get("rank") or []
    out = []
    for r in rows:
        buys, sells = int(r.get("buy_7d") or r.get("buy") or 0), int(r.get("sell_7d") or r.get("sell") or 0)
        trades = int(r.get("txs_7d") or (buys + sells))
        wr = float(r.get("winrate_7d") or r.get("winrate") or 0)
        wins = round(wr * trades)
        pnl = float(r.get("realized_profit_7d") or r.get("pnl_7d") or 0)
        out.append(LeaderboardEntry(chain=chain, wallet=r["wallet_address"], label=(r.get("name") or r.get("twitter_username") or r["wallet_address"][:6])[:24],
                                    pnl_native=pnl, wins=wins, losses=trades - wins, trades=trades, source="gmgn",
                                    extra={"pnl_is_usd": "realized_profit_7d" in r}))
    return out


async def gmgn(http: Http, chain: str, gmgn_id: str) -> list[LeaderboardEntry]:
    d = await http.get(f"https://gmgn.ai/defi/quotation/v1/rank/{gmgn_id}/wallets/7d",
                       params={"orderby": "pnl_7d", "direction": "desc"})
    return parse_gmgn(chain, d)


def load_seeds(path: str, chain: str) -> list[LeaderboardEntry]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        log.warning("seed file %s is not valid JSON", path)
        return []
    out = []
    for r in data.get(chain, []):
        wins, losses = int(r.get("wins", 0)), int(r.get("losses", 0))
        out.append(LeaderboardEntry(chain=chain, wallet=r["wallet"], label=r.get("label", r["wallet"][:6]),
                                    pnl_native=float(r.get("pnl", 0)), wins=wins, losses=losses,
                                    trades=int(r.get("trades", wins + losses)), source="seed"))
    return out
