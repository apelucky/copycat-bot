from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copycat.bot.notifier import MemoryNotifier  # noqa: E402
from copycat.chains.base import ChainAdapter, LeaderboardEntry, SecurityReport, SwapEvent, TokenInfo  # noqa: E402
from copycat.chains.registry import CHAINS, address_kind  # noqa: E402
from copycat.config import AppConfig  # noqa: E402
from copycat.core.engine import Engine  # noqa: E402
from copycat.storage.sqlite_repo import SqliteRepo  # noqa: E402

FIX = Path(__file__).parent / "fixtures"

SOL_TOK = "TokA1111111111111111111111111111111111pump"
SOL_TOK2 = "TokB2222222222222222222222222222222222pump"


class FakeDS:
    def __init__(self, chains_for: dict[str, list[str]] | None = None):
        self.chains_for = chains_for or {}

    async def search_chains(self, token: str) -> list[str]:
        return self.chains_for.get(token.lower(), [])


class FakeAdapter(ChainAdapter):
    def __init__(self, chain: str, native_usd: float = 150.0):
        self.cfg = CHAINS[chain]
        self.infos: dict[str, TokenInfo] = {}
        self.sec: dict[str, SecurityReport] = {}
        self.funders: dict[str, str | None] = {}
        self.board: list[LeaderboardEntry] = []
        self._native_usd = native_usd
        self.ds = FakeDS()
        self.enabled = True

    def status(self) -> tuple[bool, str]:
        return self.enabled, f"{self.cfg.name}: fake"

    def is_valid_address(self, addr: str) -> bool:
        return address_kind(addr) == ("evm" if self.cfg.kind == "evm" else "solana")

    async def poll(self, wallets: list[str], state: dict[str, Any]) -> list[SwapEvent]:
        return []

    async def token_info(self, token: str) -> TokenInfo | None:
        return self.infos.get(token)

    async def security_check(self, token: str) -> SecurityReport:
        return self.sec.get(token, SecurityReport(ok=True, score=5, creator=f"dev_{token[:4]}"))

    async def leaderboard(self) -> list[LeaderboardEntry]:
        return self.board

    async def funder_of(self, wallet: str) -> str | None:
        return self.funders.get(wallet)

    async def native_usd(self) -> float | None:
        return self._native_usd

    def set_price(self, token: str, price_native: float, liq: float = 200_000, symbol: str = "TOK") -> None:
        self.infos[token] = TokenInfo(self.cfg.key, token, symbol, symbol, price_native * self._native_usd, price_native, liq, 1e6)


class Clock:
    def __init__(self, t: float = 1_790_000_000):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


def board(chain: str = "solana", n: int = 4) -> list[LeaderboardEntry]:
    return [LeaderboardEntry(chain, f"Wallet{i}" + "x" * (38 - len(str(i))), f"trader{i}", 500 - i * 50, 60 - i, 40, 100 - i, "test") for i in range(1, n + 1)]


@pytest.fixture
def env():
    clock = Clock()
    cfg = AppConfig(allowed_user_ids=set(), tz_name="Africa/Lagos")
    adapters = {c: FakeAdapter(c, 150.0 if c == "solana" else (600.0 if c == "bsc" else 3000.0)) for c in CHAINS}
    repo = SqliteRepo(":memory:")
    notifier = MemoryNotifier()
    eng = Engine(cfg, repo, adapters, notifier, None, clock)
    return eng, adapters, notifier, clock


def ev(wallet: str, side: str, token: str, tokens: float, native: float, sig: str, ts: int, chain: str = "solana",
       pre: float | None = None) -> SwapEvent:
    return SwapEvent(chain, wallet, sig, ts, side, token, tokens, native, pre, source="fixture")  # type: ignore[arg-type]
