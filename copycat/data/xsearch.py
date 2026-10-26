"""X (Twitter) API v2 recent search for Group Buy tokens. Disabled when X_BEARER_TOKEN is absent.
Tweet text is untrusted: we only count/score it, never act on its contents."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .http import Http


@dataclass
class MentionReport:
    enabled: bool
    count: int = 0
    weighted: float = 0.0
    velocity_15m: float = 0.0     # weighted mentions in last 15 min
    baseline_15m: float = 0.0     # avg weighted per 15 min in the prior 105 min
    spike: bool = False
    notable: list[str] = field(default_factory=list)
    note: str = ""


def account_weight(user: dict[str, Any], now: float) -> float:
    """Older, bigger, verified accounts count more; fresh / tiny / bot-like accounts count little."""
    pm = user.get("public_metrics") or {}
    followers = int(pm.get("followers_count") or 0)
    following = int(pm.get("following_count") or 0)
    tweets = int(pm.get("tweet_count") or 0)
    try:
        created = datetime.fromisoformat(str(user.get("created_at", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        created = now
    age_days = max(0.0, (now - created) / 86400)
    w = 0.2 + min(1.0, age_days / 365) * 0.8               # age 0..1y -> 0.2..1.0
    w *= 1 + math.log10(1 + followers) / 2                  # 1k followers -> x2.5
    if age_days < 30:
        w *= 0.3
    if followers < 50 or (following > 0 and followers / max(following, 1) < 0.05):
        w *= 0.3
    if tweets > 0 and age_days > 0 and tweets / max(age_days, 1) > 300:
        w *= 0.2                                            # posts like a bot
    if user.get("verified"):
        w *= 1.5
    return round(w, 3)


def score_mentions(payload: dict[str, Any], now: float | None = None, spike_ratio: float = 3.0,
                   min_spike: float = 5.0) -> MentionReport:
    now = now or time.time()
    users = {u["id"]: u for u in ((payload.get("includes") or {}).get("users") or [])}
    tweets = payload.get("data") or []
    weighted = recent = prior = 0.0
    scored_users: dict[str, float] = {}
    for t in tweets:
        u = users.get(t.get("author_id"), {})
        w = account_weight(u, now) if u else 0.1
        weighted += w
        try:
            ts = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            ts = now
        age = now - ts
        if age <= 900:
            recent += w
        elif age <= 7200:
            prior += w
        if u:
            scored_users[u.get("username", "?")] = max(scored_users.get(u.get("username", "?"), 0), w)
    baseline = prior / 7.0
    spike = recent >= min_spike and recent >= spike_ratio * max(baseline, 0.5)
    notable = [f"@{n}" for n, _ in sorted(scored_users.items(), key=lambda kv: -kv[1])[:3]]
    return MentionReport(True, len(tweets), round(weighted, 2), round(recent, 2), round(baseline, 2), spike, notable)


class XSearch:
    URL = "https://api.x.com/2/tweets/search/recent"

    def __init__(self, http: Http, bearer: str | None):
        self.http = http
        self.bearer = bearer

    @property
    def enabled(self) -> bool:
        return bool(self.bearer)

    async def mentions(self, symbol: str | None, address: str) -> MentionReport:
        if not self.enabled:
            return MentionReport(False, note="X mentions disabled (no X_BEARER_TOKEN)")
        terms = [f'"{address}"']
        if symbol and symbol.isascii() and symbol.replace("_", "").isalnum() and len(symbol) <= 12:
            terms.append(f"${symbol}")
        q = f"({' OR '.join(terms)}) -is:retweet"
        try:
            d = await self.http.get(self.URL, params={
                "query": q, "max_results": 100, "tweet.fields": "created_at,author_id",
                "expansions": "author_id", "user.fields": "created_at,public_metrics,verified,username",
            }, headers={"Authorization": f"Bearer {self.bearer}"})
        except Exception as e:  # noqa: BLE001
            return MentionReport(False, note=f"X search failed: {str(e)[:80]}")
        return score_mentions(d or {})
