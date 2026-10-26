import pytest

from copycat.core import reports
from tests.conftest import SOL_TOK, SOL_TOK2, board, ev

UID = 42


async def setup(eng, adapters, chain="solana"):
    eng.ensure_user(UID, UID)
    adapters[chain].board = board(chain)
    await eng.refresh_leaderboard(chain)
    return eng.repo.get_following(UID, chain)["wallet"]


@pytest.mark.asyncio
async def test_auto_follow_top_and_copy_buy_quarter_size(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    assert leader.startswith("Wallet1")
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.on_event(ev(leader, "buy", SOL_TOK, 2000, 2.0, "s1", clock()))
    pos = eng.repo.find_open_position(UID, "solana", SOL_TOK, "copy")
    assert pos and pos["source"] == "copy"
    f = eng.repo.fills(UID)[-1]
    assert f["native"] == pytest.approx(0.5 + 0.0007)
    assert f["leader_price"] == pytest.approx(0.001)
    assert f["slippage_pct"] > 0
    assert eng.repo.get_balance(UID, "solana") == pytest.approx(10 - 0.5007)
    assert any("COPY BUY" in t for t in note.texts())


@pytest.mark.asyncio
async def test_duplicate_event_ignored(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    ad["solana"].set_price(SOL_TOK, 0.001)
    e = ev(leader, "buy", SOL_TOK, 2000, 2.0, "dup", clock())
    await eng.on_event(e)
    await eng.on_event(e)
    assert len(eng.repo.fills(UID)) == 1


@pytest.mark.asyncio
async def test_proportional_sell(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.on_event(ev(leader, "buy", SOL_TOK, 2000, 2.0, "b", clock()))
    tokens0 = eng.repo.find_open_position(UID, "solana", SOL_TOK)["tokens"]
    clock.tick(60)
    ad["solana"].set_price(SOL_TOK, 0.002)
    await eng.on_event(ev(leader, "sell", SOL_TOK, 1000, 2.0, "s", clock() + 60, pre=2000))
    pos = eng.repo.find_open_position(UID, "solana", SOL_TOK)
    assert pos["tokens"] == pytest.approx(tokens0 / 2)
    sell = eng.repo.fills(UID)[-1]
    assert sell["pnl_native"] > 0
    await eng.on_event(ev(leader, "sell", SOL_TOK, 1000, 2.0, "s2", clock() + 120, pre=1000))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK) is None


@pytest.mark.asyncio
async def test_two_losses_bench_switch_and_undo(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    for i, tok in enumerate([SOL_TOK, SOL_TOK2]):
        ad["solana"].set_price(tok, 0.001)
        await eng.on_event(ev(leader, "buy", tok, 2000, 2.0, f"b{i}", clock()))
        clock.tick(60)
        ad["solana"].set_price(tok, 0.0005)
        await eng.on_event(ev(leader, "sell", tok, 2000, 1.0, f"s{i}", clock(), pre=2000))
    f = eng.repo.get_following(UID, "solana")
    assert f["wallet"].startswith("Wallet2")
    assert eng.is_benched(UID, "solana", leader)
    assert any("TRADER SWITCHED: 2 losses in a row" in t for t in note.texts())
    msg = await eng.undo_switch(UID, "solana")
    assert "undone" in msg
    assert eng.repo.get_following(UID, "solana")["wallet"] == leader
    assert not eng.is_benched(UID, "solana", leader)
    clock.tick(25 * 3600)


@pytest.mark.asyncio
async def test_win_resets_streak(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    seq = [(0.0005, "l"), (0.003, "w"), (0.0005, "l")]
    for i, (exitp, _) in enumerate(seq):
        tok = f"Tok{i}" + "z" * 36
        ad["solana"].set_price(tok, 0.001)
        await eng.on_event(ev(leader, "buy", tok, 2000, 2.0, f"b{i}", clock()))
        clock.tick(60)
        ad["solana"].set_price(tok, exitp)
        await eng.on_event(ev(leader, "sell", tok, 2000, 1.0, f"s{i}", clock(), pre=2000))
    assert eng.repo.get_following(UID, "solana")["wallet"] == leader
    assert eng.repo.trader_state(UID, "solana", leader)["consec_losses"] == 1


@pytest.mark.asyncio
async def test_bench_expires_after_24h(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    await eng.switch_trader(UID, "solana", leader, "test")
    assert eng.is_benched(UID, "solana", leader)
    clock.tick(24 * 3600 + 1)
    assert not eng.is_benched(UID, "solana", leader)


@pytest.mark.asyncio
async def test_cold_trader_demoted_by_health(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    s = eng.settings(UID)
    s["loss_switch"] = 99
    eng.save_settings(UID, s)
    for i in range(8):
        tok = f"Cold{i}" + "q" * 35
        ad["solana"].set_price(tok, 0.001)
        await eng.on_event(ev(leader, "buy", tok, 2000, 2.0, f"b{i}", clock()))
        clock.tick(60)
        ad["solana"].set_price(tok, 0.0007 if i % 4 else 0.00105)
        await eng.on_event(ev(leader, "sell", tok, 2000, 1.0, f"s{i}", clock(), pre=2000))
    assert eng.repo.get_following(UID, "solana")["wallet"] != leader
    assert eng.repo.trader_state(UID, "solana", leader)["bench_reason"] == "cold trader (health)"


@pytest.mark.asyncio
async def test_copy_delay_tracker_drops_costly_trader(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    s = eng.settings(UID)
    s["max_slippage_pct"] = 50
    eng.save_settings(UID, s)
    for i in range(5):
        tok = f"Late{i}" + "r" * 35
        ad["solana"].set_price(tok, 0.0011)  # price already 10% above leader fill when we copy
        await eng.on_event(ev(leader, "buy", tok, 2000, 2.0, f"b{i}", clock()))  # leader paid 0.001
    assert eng.repo.trader_state(UID, "solana", leader)["bench_reason"] == "copy delay too costly"


@pytest.mark.asyncio
async def test_risk_skips_logged(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    ad["solana"].set_price(SOL_TOK, 0.001, liq=5000)
    await eng.on_event(ev(leader, "buy", SOL_TOK, 2000, 2.0, "b", clock()))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK) is None
    assert "liquidity" in eng.repo.skips(UID)[0]["reason"]
    from copycat.chains.base import SecurityReport
    ad["solana"].set_price(SOL_TOK2, 0.001)
    ad["solana"].sec[SOL_TOK2] = SecurityReport(ok=False, reasons=["Freeze authority (danger)"])
    await eng.on_event(ev(leader, "buy", SOL_TOK2, 2000, 2.0, "b2", clock()))
    assert "rug-check" in eng.repo.skips(UID)[0]["reason"]


@pytest.mark.asyncio
async def test_daily_loss_cap_pauses_chain(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    s = eng.settings(UID)
    s["chains"]["solana"]["daily_loss_cap"] = 0.3
    s["loss_switch"] = 99
    eng.save_settings(UID, s)
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.on_event(ev(leader, "buy", SOL_TOK, 2000, 2.0, "b", clock()))
    clock.tick(60)
    ad["solana"].set_price(SOL_TOK, 0.0003)
    await eng.on_event(ev(leader, "sell", SOL_TOK, 2000, 0.5, "s", clock(), pre=2000))
    assert any("DAILY LOSS CAP HIT" in t for t in note.texts())
    ad["solana"].set_price(SOL_TOK2, 0.001)
    await eng.on_event(ev(leader, "buy", SOL_TOK2, 2000, 2.0, "b2", clock()))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK2) is None
    assert "loss cap" in eng.repo.skips(UID)[0]["reason"]


@pytest.mark.asyncio
async def test_group_buy_entry_scale_out_and_early_exit(env):
    eng, ad, note, clock = env
    await setup(eng, ad)
    ws = [r["wallet"] for r in eng.repo.leaderboard("solana")]
    # unfollow so only group logic acts
    eng.repo.set_following(UID, "solana", None, None, clock())
    tok = SOL_TOK
    ad["solana"].set_price(tok, 0.001, symbol="GRP")
    for i, w in enumerate(ws[:2]):
        await eng.on_event(ev(w, "buy", tok, 1000, 1.0, f"g{i}", int(clock()) + i * 60))
    pos = eng.repo.find_open_position(UID, "solana", tok, "group")
    assert pos and pos["cost_native"] == pytest.approx(0.25 + 0.0007)
    assert any("GROUP BUY" in t for t in note.texts())
    init = pos["initial_tokens"]
    clock.tick(60)
    ad["solana"].set_price(tok, 0.002, symbol="GRP")
    await eng.on_event(ev(ws[2], "buy", tok, 1000, 1.0, "g2", int(clock()) + 200))
    pos = eng.repo.find_open_position(UID, "solana", tok, "group")
    assert pos["tokens"] == pytest.approx(init * 0.75)
    await eng.on_event(ev(ws[0], "sell", tok, 1000, 2.0, "x0", int(clock()) + 300))
    assert eng.repo.find_open_position(UID, "solana", tok, "group") is None
    assert any("GROUP EXIT" in t for t in note.texts())


@pytest.mark.asyncio
async def test_group_buy_shared_funder_not_entered(env):
    eng, ad, note, clock = env
    await setup(eng, ad)
    eng.repo.set_following(UID, "solana", None, None, clock())
    ws = [r["wallet"] for r in eng.repo.leaderboard("solana")]
    ad["solana"].funders = {ws[0]: "BUNDLER", ws[1]: "BUNDLER"}
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.on_event(ev(ws[0], "buy", SOL_TOK, 1000, 1.0, "a", int(clock())))
    await eng.on_event(ev(ws[1], "buy", SOL_TOK, 1000, 1.0, "b", int(clock()) + 5))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK, "group") is None
    await eng.on_event(ev(ws[2], "buy", SOL_TOK, 1000, 1.0, "c", int(clock()) + 9))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK, "group") is not None
    assert any("Shared funder" in t for t in note.texts())


@pytest.mark.asyncio
async def test_exit_alert_for_other_tracked_wallet(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.on_event(ev(leader, "buy", SOL_TOK, 2000, 2.0, "b", clock()))
    other = [r["wallet"] for r in eng.repo.leaderboard("solana")][3]
    await eng.on_event(ev(other, "sell", SOL_TOK, 500, 0.5, "o", clock(), pre=1000))
    assert any("TRADER EXIT" in t for t in note.texts())
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK) is not None  # alert only


@pytest.mark.asyncio
async def test_dev_watch_sells_copy_but_only_alerts_manual(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.on_event(ev(leader, "buy", SOL_TOK, 2000, 2.0, "b", clock()))
    await eng.manual_buy(UID, "solana", SOL_TOK, 0.1)
    dev = f"dev_{SOL_TOK[:4]}"
    assert SOL_TOK in eng.dev_wallets("solana")[dev]
    assert dev in eng.watch_list("solana")
    await eng.on_event(ev(dev, "transfer_out", SOL_TOK, 1e6, 0, "d", clock()))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK, "copy") is None
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK, "manual") is not None
    assert any("Manual position NOT auto-sold" in t for t in note.texts())


@pytest.mark.asyncio
async def test_dev_watch_auto_sells_manual_when_opted_in(env):
    eng, ad, note, clock = env
    eng.ensure_user(UID, UID)
    s = eng.settings(UID)
    s["manual_auto_sell"] = True
    eng.save_settings(UID, s)
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.manual_buy(UID, "solana", SOL_TOK, 0.1)
    await eng.on_event(ev(f"dev_{SOL_TOK[:4]}", "sell", SOL_TOK, 1e6, 10, "d", clock()))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK, "manual") is None


@pytest.mark.asyncio
async def test_manual_trades_tagged_and_not_copy_sold(env):
    eng, ad, note, clock = env
    leader = await setup(eng, ad)
    ad["solana"].set_price(SOL_TOK, 0.001, symbol="MAN")
    msg = await eng.manual_buy(UID, "solana", SOL_TOK, 0.5)
    assert "MANUAL BUY" in msg
    await eng.on_event(ev(leader, "sell", SOL_TOK, 1000, 1, "ls", clock(), pre=1000))
    assert eng.repo.find_open_position(UID, "solana", SOL_TOK, "manual") is not None
    out = await eng.manual_sell(UID, "MAN", 25)
    assert "SELL 25%" in out
    pos = eng.repo.find_open_position(UID, "solana", SOL_TOK, "manual")
    assert pos["tokens"] == pytest.approx(pos["initial_tokens"] * 0.75)
    await eng.manual_sell(UID, SOL_TOK, 100)
    closed = eng.repo.closed_positions(UID)
    assert closed[0]["source"] == "manual"
    txt = reports.pnl_text(eng, UID, "today")
    assert "manual" in txt


@pytest.mark.asyncio
async def test_manual_buy_respects_risk_caps(env):
    eng, ad, note, clock = env
    eng.ensure_user(UID, UID)
    ad["solana"].set_price(SOL_TOK, 0.001)
    msg = await eng.manual_buy(UID, "solana", SOL_TOK, 5.0)
    assert "max position" in msg
    ad["solana"].set_price(SOL_TOK2, 0.001, liq=1000)
    msg = await eng.manual_buy(UID, "solana", SOL_TOK2, 0.1)
    assert "liquidity" in msg


@pytest.mark.asyncio
async def test_pnl_includes_fees_round_trip(env):
    eng, ad, note, clock = env
    eng.ensure_user(UID, UID)
    ad["solana"].set_price(SOL_TOK, 0.001)
    await eng.manual_buy(UID, "solana", SOL_TOK, 0.5)
    await eng.manual_sell(UID, SOL_TOK, 100)
    p = eng.repo.closed_positions(UID)[0]
    assert p["realized"] < 0  # flat price → fees + gas + impact
    assert eng.repo.get_balance(UID, "solana") == pytest.approx(10 + p["realized"])
