"""Shared async HTTP client with per-host rate limiting, retries and exponential backoff."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64) CopycatPaperBot/1.0"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:160]}")
        self.status = status


class RateLimiter:
    """Simple min-interval limiter per host."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


HOST_RATES = {
    "api.dexscreener.com": 4.0,
    "api.rugcheck.xyz": 2.0,
    "api.gopluslabs.io": 1.0,
    "api.mainnet-beta.solana.com": 3.0,
    "api.twitter.com": 0.5,
    "api.x.com": 0.5,
}


class Http:
    def __init__(self, timeout: float = 20.0, retries: int = 3):
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True)
        self.retries = retries
        self._limiters: dict[str, RateLimiter] = {}

    def limiter(self, host: str) -> RateLimiter:
        if host not in self._limiters:
            self._limiters[host] = RateLimiter(HOST_RATES.get(host, 5.0))
        return self._limiters[host]

    async def request(self, method: str, url: str, *, json: Any = None, params: dict | None = None,
                      headers: dict | None = None, expect_json: bool = True) -> Any:
        host = urlparse(url).netloc
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            await self.limiter(host).wait()
            try:
                r = await self.client.request(method, url, json=json, params=params, headers=headers)
                if r.status_code == 429 or r.status_code >= 500:
                    retry_after = r.headers.get("retry-after")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else (2 ** attempt) + random.random()
                    last = HttpError(r.status_code, url, r.text)
                    if attempt < self.retries:
                        await asyncio.sleep(min(delay, 30))
                        continue
                    raise last
                if r.status_code >= 400:
                    raise HttpError(r.status_code, url, r.text)
                return r.json() if expect_json else r.text
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                if attempt < self.retries:
                    await asyncio.sleep((2 ** attempt) + random.random())
                    continue
                raise
        raise last or RuntimeError("request failed")

    async def get(self, url: str, **kw: Any) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Any:
        return await self.request("POST", url, **kw)

    async def close(self) -> None:
        await self.client.aclose()
