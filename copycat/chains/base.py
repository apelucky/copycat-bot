"""ChainAdapter interface + shared chain data types."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal

Side = Literal["buy", "sell", "transfer_out"]


@dataclass(frozen=True)
class ChainConfig:
    key: str                 # solana | base | ethereum | bsc | robinhood
    name: str
    kind: Literal["solana", "evm"]
    native: str              # SOL / ETH / BNB
    badge: str
    chain_id: int | None
    dexscreener_id: str
    goplus_id: str | None
    gmgn_id: str | None
    default_rpc: str
    explorer: str
    wrapped_native: str
    stables: tuple[str, ...]
    gas_native: float        # simulated network cost per swap, in native units
    dex_fee_pct: float       # simulated LP/router fee per swap
    start_balance: float
    default_max_position: float
    default_daily_loss_cap: float
    default_group_size: float
    manual_presets: tuple[float, ...]
    enabled_by_default: bool = True
    disabled_reason: str | None = None
    notes: str = ""
    fallback_rpcs: tuple[str, ...] = ()
    native_ref: tuple[str, str] | None = None  # (dexscreener chain, token) used to price the native coin


@dataclass
class SwapEvent:
    chain: str
    wallet: str
    signature: str
    timestamp: int
    side: Side
    token: str
    token_amount: float
    native_amount: float = 0.0          # native-equivalent spent (buy) / received (sell); 0 = unknown
    pre_balance: float | None = None    # wallet's token balance before this tx, if known
    symbol: str | None = None
    source: str = "live"

    @property
    def price_native(self) -> float | None:
        if self.native_amount > 0 and self.token_amount > 0:
            return self.native_amount / self.token_amount
        return None


@dataclass
class TokenInfo:
    chain: str
    address: str
    symbol: str
    name: str
    price_usd: float
    price_native: float          # price in the chain's native coin
    liquidity_usd: float
    mcap_usd: float | None
    pair_address: str | None = None
    dex: str | None = None
    pair_created_at: int | None = None
    url: str | None = None


@dataclass
class SecurityReport:
    ok: bool
    score: float | None = None        # 0..100, higher = riskier
    reasons: list[str] = field(default_factory=list)
    creator: str | None = None
    raw_source: str = ""


@dataclass
class LeaderboardEntry:
    chain: str
    wallet: str
    label: str
    pnl_native: float
    wins: int
    losses: int
    trades: int
    source: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


class ChainUnavailable(RuntimeError):
    """Raised when a chain is configured but not usable (missing key / no provider coverage)."""


class ChainAdapter(abc.ABC):
    cfg: ChainConfig

    @property
    def key(self) -> str:
        return self.cfg.key

    @abc.abstractmethod
    def status(self) -> tuple[bool, str]:
        """(enabled, human message)."""

    @abc.abstractmethod
    async def poll(self, wallets: list[str], state: dict[str, Any]) -> list[SwapEvent]:
        """Return new swap/transfer events for `wallets`. `state` is a JSON-able cursor dict the
        caller persists between calls (restart-safe). First call only primes cursors (no history replay)."""

    @abc.abstractmethod
    async def token_info(self, token: str) -> TokenInfo | None: ...

    @abc.abstractmethod
    async def security_check(self, token: str) -> SecurityReport: ...

    @abc.abstractmethod
    async def leaderboard(self) -> list[LeaderboardEntry]: ...

    @abc.abstractmethod
    async def funder_of(self, wallet: str) -> str | None: ...

    @abc.abstractmethod
    async def native_usd(self) -> float | None: ...

    def explorer_tx(self, sig: str) -> str:
        return f"{self.cfg.explorer}/tx/{sig}"

    def explorer_addr(self, addr: str) -> str:
        return f"{self.cfg.explorer}/address/{addr}" if self.cfg.kind == "evm" else f"{self.cfg.explorer}/account/{addr}"

    @abc.abstractmethod
    def is_valid_address(self, addr: str) -> bool: ...
