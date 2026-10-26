import pytest

from copycat.bot.commands import Commands
from copycat.chains.registry import CHAINS, address_kind, find_address
from copycat.core.settings import active_chains, merged
from tests.conftest import board, ev

UID = 7
EVM_TOK = "0x" + "ab" * 20


def test_address_kind_detection():
    assert address_kind("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263") == "solana"
    assert address_kind("0x4ed4e862860bed51a9570b96d89af5e1b0efefed") == "evm"
    assert address_kind("hello") is None
    assert find_address("check this 0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed pls") == "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"


def test_chain_registry_and_robinhood():
    assert set(CHAINS) == {"solana", "base", "ethereum", "bsc", "robinhood"}
    rh = CHAINS["robinhood"]
    assert rh.chain_id == 4663 and rh.native == "ETH" and rh.dexscreener_id == "robinhood" and rh.goplus_id == "4663"
    assert CHAINS["ethereum"].gas_native > 10 * CHAINS["base"].gas_native
    assert CHAINS["bsc"].start_balance == 2.0 and CHAINS["base"].start_balance == 0.5


def test_active_chains_and_mode_locked():
    s = merged({"active_chain": "all", "mode": "live"})
    assert s["mode"] == "paper"
    assert active_chains(s, ["solana", "base"]) == ["solana", "base"]
    assert active_chains(merged({"active_chain": "bsc"}), ["solana"]) == []


@pytest.mark.asyncio
async def test_per_chain_accounts_and_isolated_copy(env):
    eng, ad, note, clock = env
    eng.ensure_user(UID, UID)
    assert eng.repo.get_balance(UID, "solana") == 10
    assert eng.repo.get_balance(UID, "base") == 0.5
    assert eng.repo.get_balance(UID, "bsc") == 2.0
    s = eng.settings(UID)
    s["active_chain"] = "all"
    eng.save_settings(UID, s)
    ad["base"].board = board("base")
    await eng.refresh_leaderboard("base")
    lead = eng.repo.get_following(UID, "base")["wallet"]
    ad["base"].set_price(EVM_TOK, 0.00001, symbol="BASED")
    await eng.on_event(ev(lead, "buy", EVM_TOK, 10000, 0.08, "0xh1", clock(), chain="base"))
    pos = eng.repo.find_open_position(UID, "base", EVM_TOK, "copy")
    assert pos and pos["cost_native"] == pytest.approx(0.02 + CHAINS["base"].gas_native)
    assert eng.repo.get_balance(UID, "solana") == 10
    assert any("🔵 BASE" in t for t in note.texts())


@pytest.mark.asyncio
async def test_copy_only_on_active_chain(env):
    eng, ad, note, clock = env
    eng.ensure_user(UID, UID)  # active = solana
    ad["bsc"].board = board("bsc")
    await eng.refresh_leaderboard("bsc")
    eng.follow(UID, "bsc", ad["bsc"].board[0].wallet)
    ad["bsc"].set_price(EVM_TOK, 0.0001)
    await eng.on_event(ev(ad["bsc"].board[0].wallet, "buy", EVM_TOK, 1000, 0.4, "0xb", clock(), chain="bsc"))
    assert eng.repo.find_open_position(UID, "bsc", EVM_TOK) is None


@pytest.mark.asyncio
async def test_disabled_chain_message(env):
    eng, ad, note, clock = env
    ad["robinhood"].enabled = False
    eng.ensure_user(UID, UID)
    out = await eng.manual_buy(UID, "robinhood", EVM_TOK, 0.01)
    assert "disabled" in out
    cmds = Commands(eng)
    r = await cmds.handle_text(UID, UID, "/chain robinhood")
    assert "🚫" in r[0].text


@pytest.mark.asyncio
async def test_paste_ca_autodetects_evm_chain(env):
    eng, ad, note, clock = env
    eng.ensure_user(UID, UID)
    cmds = Commands(eng)
    ad["base"].set_price(EVM_TOK, 0.00001, symbol="BASED")
    for a in ad.values():
        a.ds.chains_for = {EVM_TOK: ["base"]}
    r = await cmds.handle_text(UID, UID, f"look {EVM_TOK}")
    assert "BASED" in r[0].text and "🔵 BASE" in r[0].text
    labels = [b[0] for row in r[0].buttons for b in row]
    assert "Buy 0.005 ETH" in labels and "✏️ Custom" in labels
    for a in ad.values():
        a.ds.chains_for = {EVM_TOK: ["base", "ethereum"]}
    r = await cmds.handle_text(UID, UID, EVM_TOK)
    assert "Which EVM chain" in r[0].text


@pytest.mark.asyncio
async def test_commands_buy_sell_and_card_sell_buttons(env):
    eng, ad, note, clock = env
    cmds = Commands(eng)
    sol = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    ad["solana"].set_price(sol, 0.0000001, liq=5e6, symbol="Bonk")
    r = await cmds.handle_text(UID, UID, f"/buy {sol} 0.2")
    assert "MANUAL BUY" in r[0].text
    r = await cmds.handle_text(UID, UID, sol)
    labels = [b[0] for row in r[0].buttons for b in row]
    assert {"Sell 25%", "Sell 50%", "Sell 100%"} <= set(labels)
    r = await cmds.handle_text(UID, UID, "/sell Bonk 50")
    assert "SELL 50%" in r[0].text
    r = await cmds.handle_callback(UID, UID, f"mbc:solana:{sol}")
    r = await cmds.handle_text(UID, UID, "0.1")
    assert "MANUAL BUY" in r[0].text


@pytest.mark.asyncio
async def test_access_gate(env):
    eng, ad, note, clock = env
    eng.cfg.allowed_user_ids = {1}
    r = await Commands(eng).handle_text(2, 2, "/start")
    assert "Not authorised" in r[0].text


@pytest.mark.asyncio
async def test_all_screens_render(env):
    eng, ad, note, clock = env
    cmds = Commands(eng)
    ad["solana"].board = board()
    for c in ["/start", "/status", "/leaderboard", "/positions", "/pnl 30d", "/log", "/settings", "/bench", "/mode",
              "/groupbuy", "/advisor", "/help", "/chain", "/hours", "/morning", "/export", "/chart", "/set copy_mult 0.3"]:
        r = await cmds.handle_text(UID, UID, c)
        assert r and (r[0].text or r[0].photo or r[0].document), c
    assert eng.settings(UID)["copy_mult"] == 0.3
    r = await cmds.handle_text(UID, UID, "/set copy_mult 9")
    assert "between" in r[0].text


@pytest.mark.asyncio
async def test_trade_card_and_pnl_card_render(env, tmp_path):
    from PIL import Image
    eng, ad, note, clock = env
    cmds = Commands(eng)
    sol = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    ad["solana"].set_price(sol, 0.0000001, liq=5e6, symbol="Bonk")
    await cmds.handle_text(UID, UID, f"/buy {sol} 0.2")
    clock.tick(600)
    ad["solana"].set_price(sol, 0.00000015, liq=5e6, symbol="Bonk")
    pid = eng.repo.open_positions(UID)[0]["id"]
    r = await cmds.handle_callback(UID, UID, f"sellp:{pid}:100")
    assert r[0].buttons == [[("🖼 Trade card", f"tcard:{pid}")]]
    r = await cmds.handle_callback(UID, UID, f"tcard:{pid}")
    im = Image.open(r[0].photo)
    assert im.size == (1080, 1350)
    r = await cmds.handle_text(UID, UID, "/pnlcard today")
    assert Image.open(r[0].photo).size[0] == 1080
