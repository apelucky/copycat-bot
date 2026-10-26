from copycat.chains.base import LeaderboardEntry, SecurityReport, TokenInfo
from copycat.core import ranking, risk


def E(label, pnl, wins, losses, src="kolscan"):
    return LeaderboardEntry("solana", label * 8, label, pnl, wins, losses, wins + losses, src)


def test_filters_bots_bait_and_thin():
    rows = ranking.rank([E("good", 400, 60, 40), E("bot", 900, 3000, 1000), E("bait", 600, 40, 0),
                         E("thin", 50, 5, 3), E("wash", 2, 400, 400), E("neg", -10, 20, 40)])
    flagged = {r["label"]: r["flagged"] for r in rows}
    assert flagged["good"] is None
    assert "bot" in flagged["bot"]
    assert "bait" in flagged["bait"]
    assert "too few" in flagged["thin"]
    assert "wash" in flagged["wash"]
    assert "negative" in flagged["neg"]
    assert rows[0]["label"] == "good" and rows[0]["rank"] == 1


def test_rank_orders_by_score():
    rows = ranking.rank([E("a", 100, 30, 30), E("b", 1000, 70, 30), E("c", 1000, 40, 60)])
    assert [r["label"] for r in rows][:3] == ["b", "c", "a"]


def test_health_score():
    good = [{"realized": 0.1, "initial_cost": 0.5}] * 8
    bad = [{"realized": -0.2, "initial_cost": 0.5}] * 8
    assert ranking.health(good)[0] > 0.7
    assert ranking.health(bad)[0] < 0.35
    assert ranking.health([]) == (None, 0)


def base_input(**kw):
    tok = TokenInfo("solana", "x", "X", "X", 1, 0.001, 100_000, 1e6)
    d = dict(paused=False, chain_paused=False, realized_today=0, daily_loss_cap=1, size=0.5, max_position=0.5, token=tok,
             min_liq_usd=20_000, rug_check=True, security=SecurityReport(ok=True), impact_pct=1.0, max_slippage_pct=3.0)
    d.update(kw)
    return risk.RiskInput(**d)


def test_risk_gate():
    assert risk.check(base_input()) is None
    assert risk.check(base_input(paused=True)) == "bot paused"
    assert "loss cap" in risk.check(base_input(realized_today=-1.0))
    assert "max position" in risk.check(base_input(size=0.6))
    assert "liquidity" in risk.check(base_input(token=TokenInfo("solana", "x", "X", "X", 1, 1, 5_000, 1)))
    assert "rug-check" in risk.check(base_input(security=SecurityReport(ok=False, reasons=["honeypot"])))
    assert "slippage" in risk.check(base_input(impact_pct=5.0))
    assert risk.check(base_input(token=None)) == "no price/liquidity data"
    assert risk.check(base_input(worst_hour=True)) == "paused during worst hour"
    assert risk.check(base_input(worst_hour=True, manual=True)) is None
