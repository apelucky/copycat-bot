"""Wiring: build adapters, repo, engine from config."""
from __future__ import annotations

from typing import Any

from .bot.notifier import Notifier
from .chains.base import ChainAdapter
from .chains.evm import EvmAdapter
from .chains.registry import CHAINS, EVM_CHAINS
from .chains.solana import SolanaAdapter
from .config import AppConfig
from .core.engine import Engine
from .data.dexscreener import DexScreener
from .data.http import Http
from .data.xsearch import XSearch
from .storage.sqlite_repo import SqliteRepo


def build(cfg: AppConfig, notifier: Notifier, repo: Any = None, http: Http | None = None) -> tuple[Engine, Http]:
    http = http or Http()
    ds = DexScreener(http)
    adapters: dict[str, ChainAdapter] = {
        "solana": SolanaAdapter(CHAINS["solana"], http, ds, cfg.helius_api_key, cfg.rpc_overrides.get("solana"), cfg.seed_file)
    }
    for c in EVM_CHAINS:
        adapters[c] = EvmAdapter(CHAINS[c], http, ds, cfg.rpc_overrides.get(c), cfg.etherscan_api_key, cfg.seed_file)
    repo = repo or SqliteRepo(cfg.db_path)
    engine = Engine(cfg, repo, adapters, notifier, XSearch(http, cfg.x_bearer_token))
    return engine, http
