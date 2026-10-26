"""pump.fun coin metadata (creator, bonding-curve status). Cloudflare often blocks server IPs, so failures
are treated as 'no data' and RugCheck's creator field is used instead."""
from __future__ import annotations

from typing import Any

from .http import Http


class PumpFun:
    BASE = "https://frontend-api-v3.pump.fun"

    def __init__(self, http: Http):
        self.http = http
        self.available = True

    async def coin(self, mint: str) -> dict[str, Any] | None:
        if not self.available or not mint.endswith("pump"):
            return None
        try:
            return await self.http.get(f"{self.BASE}/coins/{mint}")
        except Exception:
            # One failure (403 / Cloudflare) disables further attempts for this process.
            self.available = False
            return None
