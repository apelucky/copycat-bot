import json

import pytest

from copycat.chains.evm import EvmAdapter
from copycat.chains.registry import CHAINS
from copycat.services.runner import Runner
from copycat.storage.sqlite_repo import SqliteRepo
from tests.conftest import FIX, SOL_TOK, board, ev


def test_robinhood_learns_weth_and_prices_native_from_ethereum():
    a = EvmAdapter(CHAINS["robinhood"], http=None, ds=None)  # type: ignore[arg-type]
    assert a.wrapped == ""
    pairs = json.loads((FIX / "ds_robinhood.json").read_text())
    pairs.append({"quoteToken": {"symbol": "WETH", "address": "0x" + "42" * 20}})
    a._learn_wrapped(pairs)
    assert a.wrapped.startswith("0x") and int(a.wrapped, 16) != 0
    assert CHAINS["robinhood"].native_ref[0] == "ethereum"
    assert a.status()[0] and "Robinhood" in a.status()[1]


def test_evm_disabled_reason_message():
    a = EvmAdapter(CHAINS["base"], http=None, ds=None)  # type: ignore[arg-type]
    a.disabled_reason = "not yet supported by data providers"
    ok, msg = a.status()
    assert not ok and "not yet supported" in msg


@pytest.mark.asyncio
async def test_runner_poll_persists_cursor_and_feeds_engine(env, tmp_path):
    eng, ad, note, clock = env
    eng.ensure_user(1, 1)
    ad["solana"].board = board()
    await eng.refresh_leaderboard("solana")
    leader = eng.repo.get_following(1, "solana")["wallet"]
    ad["solana"].set_price(SOL_TOK, 0.001)

    async def poll(wallets, state):
        state["x"] = state.get("x", 0) + 1
        return [ev(leader, "buy", SOL_TOK, 1000, 1.0, f"p{state['x']}", int(clock()))] if leader in wallets else []

    ad["solana"].poll = poll  # type: ignore[method-assign]
    r = Runner(eng)
    assert await r.poll_chain_once("solana") == 1
    assert eng.repo.kv_get("cursor:solana") == {"x": 1}
    assert eng.repo.find_open_position(1, "solana", SOL_TOK, "copy") is not None
    assert eng.repo.kv_get("heartbeat")["solana"]["events"] == 1


def test_sqlite_restart_safe(tmp_path):
    p = str(tmp_path / "c.db")
    r = SqliteRepo(p)
    r.upsert_user(5, 5, {"a": 1})
    r.ensure_account(5, "solana", 10)
    r.add_balance(5, "solana", -1.5)
    r.kv_set("groupbuy", [{"chain": "solana", "token": "T", "first_ts": 1, "buys": []}])
    r2 = SqliteRepo(p)
    assert r2.get_balance(5, "solana") == 8.5 and r2.kv_get("groupbuy")[0]["token"] == "T"
