"""GoPlus Security token check (EVM): honeypot, taxes, owner powers."""
from __future__ import annotations

from ..chains.base import SecurityReport
from .http import Http, HttpError

BASE = "https://api.gopluslabs.io/api/v1"


def parse_goplus(d: dict, max_tax: float = 0.10) -> SecurityReport:
    reasons: list[str] = []
    flag = lambda k: str(d.get(k, "0")) == "1"  # noqa: E731
    if flag("is_honeypot"):
        reasons.append("honeypot")
    if flag("cannot_sell_all"):
        reasons.append("cannot sell all")
    if flag("cannot_buy"):
        reasons.append("cannot buy")
    if flag("owner_change_balance"):
        reasons.append("owner can change balances")
    if flag("hidden_owner"):
        reasons.append("hidden owner")
    if flag("selfdestruct"):
        reasons.append("selfdestruct")
    if flag("is_blacklisted") and flag("is_mintable"):
        reasons.append("blacklist + mintable")
    for k in ("buy_tax", "sell_tax"):
        try:
            t = float(d.get(k) or 0)
        except ValueError:
            t = 0
        if t > max_tax:
            reasons.append(f"{k.replace('_', ' ')} {t:.0%}")
    score = min(100.0, 25.0 * len(reasons))
    return SecurityReport(ok=not reasons, score=score, reasons=reasons,
                          creator=(d.get("creator_address") or None), raw_source="goplus")


class GoPlus:
    def __init__(self, http: Http):
        self.http = http

    async def check(self, chain_id: str, token: str) -> SecurityReport:
        try:
            d = await self.http.get(f"{BASE}/token_security/{chain_id}", params={"contract_addresses": token.lower()})
        except HttpError as e:
            return SecurityReport(ok=False, reasons=[f"goplus unavailable ({e.status})"], raw_source="goplus")
        res = (d or {}).get("result") or {}
        item = res.get(token.lower()) or (next(iter(res.values())) if res else None)
        if not item:
            return SecurityReport(ok=False, reasons=["goplus: token not indexed yet"], raw_source="goplus")
        return parse_goplus(item)
