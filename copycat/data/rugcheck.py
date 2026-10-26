"""RugCheck (Solana) public API: risk summary + creator."""
from __future__ import annotations

from ..chains.base import SecurityReport
from .http import Http, HttpError

BASE = "https://api.rugcheck.xyz/v1"


def summary_to_report(d: dict, max_score: float = 60) -> SecurityReport:
    reasons = [f"{r.get('name')} ({r.get('level')})" for r in d.get("risks", []) if r.get("level") in ("danger", "warn")]
    score = d.get("score_normalised")
    danger = [r for r in d.get("risks", []) if r.get("level") == "danger"]
    ok = not d.get("rugged") and not danger and (score is None or float(score) < max_score)
    return SecurityReport(ok=ok, score=float(score) if score is not None else None, reasons=reasons, raw_source="rugcheck")


class RugCheck:
    def __init__(self, http: Http):
        self.http = http
        self._creator: dict[str, str | None] = {}

    async def check(self, mint: str) -> SecurityReport:
        try:
            d = await self.http.get(f"{BASE}/tokens/{mint}/report/summary")
        except HttpError as e:
            return SecurityReport(ok=False, reasons=[f"rugcheck unavailable ({e.status})"], raw_source="rugcheck")
        rep = summary_to_report(d)
        rep.creator = await self.creator(mint)
        return rep

    async def creator(self, mint: str) -> str | None:
        if mint in self._creator:
            return self._creator[mint]
        try:
            d = await self.http.get(f"{BASE}/tokens/{mint}/report")
            c = d.get("creator")
        except Exception:
            c = None
        self._creator[mint] = c
        return c
