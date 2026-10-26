"""Static per-chain configuration + address-format chain detection."""
from __future__ import annotations

import re

from .base import ChainConfig

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_SOL = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

CHAINS: dict[str, ChainConfig] = {
    "solana": ChainConfig(
        key="solana", name="Solana", kind="solana", native="SOL", badge="◎ SOL", chain_id=None,
        dexscreener_id="solana", goplus_id="solana", gmgn_id="sol",
        default_rpc="https://api.mainnet-beta.solana.com", explorer="https://solscan.io",
        wrapped_native=SOL_MINT, stables=(USDC_SOL, USDT_SOL),
        gas_native=0.0007, dex_fee_pct=0.01, start_balance=10.0,
        default_max_position=0.5, default_daily_loss_cap=1.0, default_group_size=0.25,
        manual_presets=(0.1, 0.5, 1.0),
    ),
    "base": ChainConfig(
        key="base", name="Base", kind="evm", native="ETH", badge="🔵 BASE", chain_id=8453,
        dexscreener_id="base", goplus_id="8453", gmgn_id="base",
        default_rpc="https://base.drpc.org", fallback_rpcs=("https://1rpc.io/base", "https://mainnet.base.org"), explorer="https://basescan.org",
        wrapped_native="0x4200000000000000000000000000000000000006",
        stables=("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",),
        gas_native=0.00003, dex_fee_pct=0.006, start_balance=0.5,
        default_max_position=0.03, default_daily_loss_cap=0.05, default_group_size=0.015,
        manual_presets=(0.005, 0.02, 0.05),
    ),
    "ethereum": ChainConfig(
        key="ethereum", name="Ethereum", kind="evm", native="ETH", badge="⟠ ETH", chain_id=1,
        dexscreener_id="ethereum", goplus_id="1", gmgn_id="eth",
        default_rpc="https://eth.drpc.org", fallback_rpcs=("https://1rpc.io/eth",), explorer="https://etherscan.io",
        wrapped_native="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        stables=("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0xdac17f958d2ee523a2206206994597c13d831ec7"),
        gas_native=0.0012, dex_fee_pct=0.006, start_balance=0.5,
        default_max_position=0.05, default_daily_loss_cap=0.05, default_group_size=0.025,
        manual_presets=(0.01, 0.03, 0.1),
        notes="Ethereum gas is high: ~0.0012 ETH per simulated swap (~150k gas @ ~8 gwei).",
    ),
    "bsc": ChainConfig(
        key="bsc", name="BNB Chain", kind="evm", native="BNB", badge="🟡 BSC", chain_id=56,
        dexscreener_id="bsc", goplus_id="56", gmgn_id="bsc",
        default_rpc="https://1rpc.io/bnb", fallback_rpcs=("https://bsc-dataseed.bnbchain.org",), explorer="https://bscscan.com",
        wrapped_native="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        stables=("0x55d398326f99059ff775485246999027b3197955", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"),
        gas_native=0.00015, dex_fee_pct=0.0075, start_balance=2.0,
        default_max_position=0.1, default_daily_loss_cap=0.2, default_group_size=0.05,
        manual_presets=(0.02, 0.1, 0.25),
    ),
    "robinhood": ChainConfig(
        key="robinhood", name="Robinhood Chain", kind="evm", native="ETH", badge="🪶 HOOD", chain_id=4663,
        dexscreener_id="robinhood", goplus_id="4663", gmgn_id=None,
        default_rpc="https://rpc.mainnet.chain.robinhood.com", fallback_rpcs=("https://robinhood.drpc.org",),
        native_ref=("ethereum", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"), explorer="https://robinhoodchain.blockscout.com",
        wrapped_native="", stables=(),
        gas_native=0.00001, dex_fee_pct=0.006, start_balance=0.5,
        default_max_position=0.03, default_daily_loss_cap=0.05, default_group_size=0.015,
        manual_presets=(0.005, 0.02, 0.05),
        notes=("Arbitrum Orbit L2, mainnet live since 2026-07-01, chain id 4663, gas in ETH. "
               "DexScreener (chainId 'robinhood') and GoPlus (4663) cover it; GMGN/Kolscan have no "
               "leaderboard, so traders come from the seed list or /follow. Wrapped-ETH address is "
               "auto-detected from DexScreener pairs."),
    ),
}

CHAIN_ORDER = ["solana", "base", "ethereum", "bsc", "robinhood"]
EVM_CHAINS = [c for c in CHAIN_ORDER if CHAINS[c].kind == "evm"]

_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")


def address_kind(addr: str) -> str | None:
    """'solana' for base58 mints, 'evm' for 0x addresses, None otherwise."""
    a = addr.strip()
    if _EVM.match(a):
        return "evm"
    if _B58.match(a):
        return "solana"
    return None


CA_PATTERN = re.compile(r"(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})")


_B58_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode_len(s: str) -> int | None:
    n = 0
    for ch in s:
        i = _B58_ALPHA.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return len(s) - len(s.lstrip("1")) + len(body)


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58_ALPHA[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def is_solana_pubkey(s: str) -> bool:
    return bool(_B58.match(s)) and b58decode_len(s) == 32


def find_address(text: str) -> str | None:
    m = CA_PATTERN.search(text or "")
    return m.group(1) if m else None
