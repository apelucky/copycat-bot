"""Environment-driven configuration. Nothing here holds private keys: the bot is paper-only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else default


def _ids(raw: str | None) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


@dataclass
class AppConfig:
    telegram_token: str | None = None
    helius_api_key: str | None = None
    xai_api_key: str | None = None
    xai_model: str = "grok-4"
    x_bearer_token: str | None = None
    etherscan_api_key: str | None = None
    allowed_user_ids: set[int] = field(default_factory=set)
    tz_name: str = "Africa/Lagos"
    db_path: str = "copycat.db"
    seed_file: str = "seeds/wallets.json"
    poll_seconds: int = 25
    leaderboard_refresh_minutes: int = 360
    tracked_top_n: int = 15
    morning_report_hour: int = 9
    rpc_overrides: dict[str, str] = field(default_factory=dict)
    enabled_chains_override: set[str] | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @classmethod
    def from_env(cls) -> "AppConfig":
        rpc: dict[str, str] = {}
        for chain in ("solana", "base", "ethereum", "bsc", "robinhood"):
            v = _env(f"RPC_URL_{chain.upper()}")
            if v:
                rpc[chain] = v
        enabled = _env("ENABLED_CHAINS")
        return cls(
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            helius_api_key=_env("HELIUS_API_KEY"),
            xai_api_key=_env("XAI_API_KEY"),
            xai_model=_env("XAI_MODEL", "grok-4") or "grok-4",
            x_bearer_token=_env("X_BEARER_TOKEN"),
            etherscan_api_key=_env("ETHERSCAN_API_KEY"),
            allowed_user_ids=_ids(_env("ALLOWED_USER_IDS")),
            tz_name=_env("TZ", "Africa/Lagos") or "Africa/Lagos",
            db_path=_env("DB_PATH", "copycat.db") or "copycat.db",
            seed_file=_env("SEED_FILE", "seeds/wallets.json") or "seeds/wallets.json",
            poll_seconds=int(_env("POLL_SECONDS", "25") or 25),
            leaderboard_refresh_minutes=int(_env("LEADERBOARD_REFRESH_MINUTES", "360") or 360),
            tracked_top_n=int(_env("TRACKED_TOP_N", "15") or 15),
            morning_report_hour=int(_env("MORNING_REPORT_HOUR", "9") or 9),
            rpc_overrides=rpc,
            enabled_chains_override={c.strip() for c in enabled.split(",")} if enabled else None,
        )
