"""Postgres repository: Supabase-style URLs, restart safety, and parity with SQLite."""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from psycopg.conninfo import conninfo_to_dict

from copycat.app import build
from copycat.bot.notifier import MemoryNotifier
from copycat.chains.registry import CHAINS
from copycat.config import AppConfig
from copycat.core.engine import Engine
from copycat.services.runner import Runner
from copycat.storage.factory import open_repo
from copycat.storage.postgres_repo import PostgresRepo, normalize_database_url
from copycat.storage.sqlite_repo import SqliteRepo
from tests.conftest import SOL_TOK, Clock, FakeAdapter, board, ev

_TABLES = (
    "users, accounts, positions, fills, skipped, following, trader_state, "
    "leaderboard, leader_events, leader_holdings, funders, kv"
)
_LEADER_EVENT = {
    "chain": "solana", "signature": "s1", "wallet": "W", "token": "T", "side": "buy",
    "timestamp": 10, "token_amount": 5.0, "native_amount": 1.0, "symbol": "TOK",
}


def _pg_url() -> str:
    return os.getenv("TEST_DATABASE_URL", "postgresql:///copycat_test")


@pytest.fixture
def pg():
    url = _pg_url()
    try:
        repo = PostgresRepo(url)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"local Postgres not available ({e})")
    with repo.lock:
        repo.conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY")
    yield repo
    try:
        if repo.conn is not None and not repo.conn.closed:
            with repo.lock:
                repo.conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY")
    finally:
        repo.close()


def _engines(repo, clock=None):
    clock = clock or Clock()
    cfg = AppConfig(tz_name="Africa/Lagos", database_url=getattr(repo, "url", None))
    adapters = {c: FakeAdapter(c, 150.0 if c == "solana" else 3000.0) for c in CHAINS}
    note = MemoryNotifier()
    eng = Engine(cfg, repo, adapters, note, None, clock)
    return eng, adapters, note, clock


def exercise(repo) -> dict:
    assert repo.ping() is True
    assert repo.upsert_user(5, 9, {"paused": False, "copy_mult": 0.25}) is True
    assert repo.upsert_user(5, 10, {"paused": True}) is False
    assert repo.get_settings(5)["paused"] is False
    repo.save_settings(5, {"paused": True, "copy_mult": 0.5})
    assert repo.list_users() == [{"user_id": 5, "chat_id": 10}]

    repo.ensure_account(5, "solana", 10)
    repo.ensure_account(5, "solana", 99)
    assert repo.get_balance(5, "solana") == 10
    assert repo.add_balance(5, "solana", -1.5) == 8.5
    assert repo.get_account(5, "solana")["start_balance"] == 10

    open_id = repo.open_position(
        user_id=5, chain="solana", token="OPEN", symbol="OP", source="manual", trader=None,
        tokens=3, cost_native=1, initial_tokens=3, initial_cost=1, fees=0.01, opened_at=100, meta={"steps": 1},
    )
    closed_id = repo.open_position(
        user_id=5, chain="solana", token="CLOSED", symbol="CL", source="copy", trader="W",
        tokens=4, cost_native=2, initial_tokens=4, initial_cost=2, fees=0, opened_at=100, meta={},
    )
    repo.update_position(closed_id, status="closed", closed_at=200, realized=0.4, tokens=0, cost_native=0, meta={"steps": 2})
    fill_id = repo.add_fill(
        user_id=5, chain="solana", position_id=open_id, side="buy", token="OPEN", symbol="OP",
        source="manual", trader=None, tokens=3, native=1, price=0.3, leader_price=None,
        slippage_pct=None, impact_pct=0.1, fee_native=0.01, gas_native=0.001, pnl_native=None,
        ts=100, leader_sig=None, reason="manual buy",
    )
    repo.add_skip(5, "solana", "OPEN", "OP", None, "low liq", 150)

    repo.set_following(5, "solana", "W", "alpha", 100)
    repo.set_following(5, "base", "0xabc", "beta", 100)
    assert repo.get_following(5, "solana")["label"] == "alpha"
    assert repo.followers_of("solana", "W") == [5]
    repo.set_following(5, "solana", None, None, 200)

    repo.save_trader_state(5, "solana", "W", consec_losses=2, benched_until=9_999_999, bench_reason="2 losses")
    assert repo.trader_state(5, "solana", "missing")["consec_losses"] == 0

    rows = [
        {"wallet": "A", "label": "a", "pnl": 10, "wins": 6, "losses": 4, "trades": 10, "score": 1.5, "source": "test", "rank": 2, "flagged": None},
        {"wallet": "B", "label": "b", "pnl": 3, "wins": 1, "losses": 1, "trades": 2, "score": 0.5, "source": "test", "rank": 1, "flagged": "bait"},
    ]
    repo.replace_leaderboard("solana", rows, 50)
    assert [r["wallet"] for r in repo.leaderboard("solana")] == ["A", "B"]
    repo.replace_leaderboard("solana", [], 60)
    assert repo.leaderboard("solana") == []
    repo.replace_leaderboard("solana", rows, 70)

    assert repo.record_leader_event(_LEADER_EVENT) is True
    assert repo.record_leader_event(_LEADER_EVENT) is False

    repo.set_leader_holding("solana", "W", "T", -3)
    assert repo.leader_holding("solana", "W", "T") == 0
    repo.set_leader_holding("solana", "W", "T", 5)
    repo.set_leader_holding("solana", "W2", "T", 0)
    assert repo.get_funder("solana", "W") == (False, None)
    repo.set_funder("solana", "W", "FUND", 10)
    repo.set_funder("solana", "W", None, 11)

    repo.kv_set("groupbuy", [{"chain": "solana", "token": "T", "first_ts": 1, "buys": [], "alerted_at": 2, "exited": False}])
    repo.kv_set("morning_sent", "2026-10-26")
    repo.kv_set("cursor:solana", {"W": "sigabc"})
    repo.kv_set("undo:5:solana", None)
    return {"open_id": open_id, "closed_id": closed_id, "fill_id": fill_id}


def assert_persisted(repo, ids: dict) -> None:
    assert repo.get_settings(5)["paused"] is True
    assert repo.get_settings(5)["copy_mult"] == 0.5
    assert repo.get_balance(5, "solana") == 8.5
    assert repo.get_account(5, "solana")["start_balance"] == 10

    pos = repo.get_position(ids["open_id"])
    assert pos["token"] == "OPEN" and pos["status"] == "open"
    assert pos["meta"] == {"steps": 1}
    assert repo.find_open_position(5, "solana", "OPEN", "manual")["id"] == ids["open_id"]
    closed = repo.closed_positions(5, since_ts=0, chain="solana", trader="W", source="copy", limit=5)
    assert [p["id"] for p in closed] == [ids["closed_id"]]
    assert closed[0]["meta"] == {"steps": 2}
    assert closed[0]["realized"] == pytest.approx(0.4)

    fills = repo.fills(5, 0, "solana")
    assert [f["id"] for f in fills] == [ids["fill_id"]]
    assert fills[0]["pnl_native"] is None
    assert repo.skips(5, 0)[0]["reason"] == "low liq"

    assert repo.get_following(5, "solana") is None
    assert repo.get_following(5, "base")["label"] == "beta"
    assert repo.followers_of("solana", "W") == []
    assert repo.followers_of("base", "0xabc") == [5]
    assert repo.all_followed("base") == ["0xabc"]
    assert repo.all_followed("solana") == []

    st = repo.trader_state(5, "solana", "W")
    assert st["consec_losses"] == 2 and st["benched_until"] == 9_999_999 and st["bench_reason"] == "2 losses"
    assert repo.benched(5, "solana", 1000)[0]["wallet"] == "W"
    assert repo.benched(5, "solana", 9_999_999) == []

    board_rows = repo.leaderboard("solana")
    assert [(r["wallet"], r["flagged"], r["rank"]) for r in board_rows] == [("A", None, 2), ("B", "bait", 1)]
    assert repo.record_leader_event(_LEADER_EVENT) is False
    assert len(repo.leader_events("solana", 0, "buy")) == 1
    assert repo.leader_holding("solana", "W", "T") == 5
    assert repo.holders_of("solana", "T", ["W", "W2"]) == ["W"]
    assert repo.holders_of("solana", "T", []) == []
    assert repo.get_funder("solana", "W") == (True, None)

    assert repo.kv_get("morning_sent") == "2026-10-26"
    assert repo.kv_get("cursor:solana") == {"W": "sigabc"}
    assert repo.kv_get("undo:5:solana") is None
    assert repo.kv_get("missing", "d") == "d"
    assert repo.kv_get("groupbuy")[0]["alerted_at"] == 2
    assert repo.kv_get("groupbuy")[0]["exited"] is False


def test_sqlite_contract_survives_reopen(tmp_path):
    path = str(tmp_path / "c.db")
    repo = SqliteRepo(path)
    ids = exercise(repo)
    assert_persisted(repo, ids)
    assert_persisted(SqliteRepo(path), ids)


def test_postgres_contract_survives_new_connection(pg):
    ids = exercise(pg)
    assert_persisted(pg, ids)
    assert pg.conn.prepare_threshold is None
    again = PostgresRepo(pg.url)
    try:
        assert_persisted(again, ids)
        assert again.conn.prepare_threshold is None
    finally:
        again.close()


def test_normalize_supabase_pooler_and_password():
    info = conninfo_to_dict(normalize_database_url(
        "postgres://postgres.abcd:p%40ss@aws-0-eu-central-1.pooler.supabase.com:6543/postgres?pgbouncer=true"))
    assert info["sslmode"] == "require"
    assert info["channel_binding"] == "disable"
    assert info["gssencmode"] == "disable"
    assert info["user"] == "postgres.abcd"
    assert info["password"] == "p@ss"
    assert int(info["port"]) == 6543
    assert info["host"].endswith("pooler.supabase.com")
    assert "pgbouncer" not in info


def test_normalize_keeps_explicit_sslmode_and_strips_quotes():
    info = conninfo_to_dict(normalize_database_url(
        '"postgresql://u:secret@db.abc.supabase.co:5432/postgres?sslmode=verify-full"'))
    assert info["sslmode"] == "verify-full"
    assert info["host"] == "db.abc.supabase.co"
    assert info["channel_binding"] == "disable"


def test_normalize_leaves_local_postgres_without_required_ssl():
    info = conninfo_to_dict(normalize_database_url("postgresql:///copycat_test"))
    assert info.get("sslmode") in (None, "prefer", "disable", "")


def test_open_repo_sqlite_without_database_url(tmp_path):
    repo = open_repo(AppConfig(db_path=str(tmp_path / "a.db")))
    assert isinstance(repo, SqliteRepo)
    assert repo.ping() is True


def test_open_repo_redacts_password():
    cfg = AppConfig(database_url="postgresql://postgres.abc:SuperSecret9@127.0.0.1:1/postgres")
    with pytest.raises(SystemExit) as ei:
        open_repo(cfg)
    text = str(ei.value)
    assert "SuperSecret9" not in text
    assert "127.0.0.1" in text


def test_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres")
    assert AppConfig.from_env().database_url.startswith("postgresql://")
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert AppConfig.from_env().database_url is None


@pytest.mark.asyncio
async def test_build_uses_postgres(pg):
    cfg = AppConfig(database_url=pg.url, tz_name="Africa/Lagos", seed_file="seeds/wallets.json")
    eng, http = build(cfg, MemoryNotifier())
    try:
        assert isinstance(eng.repo, PostgresRepo)
        assert eng.repo.ping() is True
        assert eng.repo.conn.prepare_threshold is None
    finally:
        eng.repo.close()
        await http.close()


@pytest.mark.asyncio
async def test_restart_does_not_replay_copy_or_alerts(pg):
    eng, ad, note, clock = _engines(pg)
    eng.ensure_user(42, 42)
    ad["solana"].board = board()
    await eng.refresh_leaderboard("solana")
    leader = eng.repo.get_following(42, "solana")["wallet"]
    ad["solana"].set_price(SOL_TOK, 0.001)

    async def poll(wallets, state):
        state["x"] = 1
        return [ev(leader, "buy", SOL_TOK, 2000, 2.0, "p1", int(clock()))] if leader in wallets else []

    ad["solana"].poll = poll  # type: ignore[method-assign]
    assert await Runner(eng).poll_chain_once("solana") == 1
    pos = eng.repo.find_open_position(42, "solana", SOL_TOK, "copy")
    assert pos is not None
    balance = eng.repo.get_balance(42, "solana")
    assert any("COPY BUY" in t for t in note.texts())

    repo2 = PostgresRepo(pg.url)
    try:
        assert repo2.kv_get("cursor:solana") == {"x": 1}
        note2 = MemoryNotifier()
        eng2 = Engine(AppConfig(tz_name="Africa/Lagos"), repo2, ad, note2, None, clock)
        await eng2.on_event(ev(leader, "buy", SOL_TOK, 2000, 2.0, "p1", int(clock())))
        assert note2.texts() == []
        assert repo2.get_balance(42, "solana") == pytest.approx(balance)
        again = repo2.find_open_position(42, "solana", SOL_TOK, "copy")
        assert again["id"] == pos["id"]
        assert again["tokens"] == pytest.approx(pos["tokens"])
        assert len(repo2.fills(42)) == 1
        repo2.save_trader_state(42, "solana", leader, benched_until=int(clock()) + 10_000, bench_reason="2 losses", consec_losses=2)
    finally:
        repo2.close()

    repo3 = PostgresRepo(pg.url)
    try:
        eng3 = Engine(AppConfig(tz_name="Africa/Lagos"), repo3, ad, MemoryNotifier(), None, clock)
        assert eng3.is_benched(42, "solana", leader)
        assert repo3.trader_state(42, "solana", leader)["bench_reason"] == "2 losses"
    finally:
        repo3.close()


@pytest.mark.asyncio
async def test_group_alert_not_repeated_after_restart(pg):
    eng, ad, note, clock = _engines(pg)
    eng.ensure_user(42, 42)
    eng.repo.kv_set("tracked:solana", ["Wa", "Wb"])
    ad["solana"].set_price(SOL_TOK, 0.001)
    e1 = ev("Wa", "buy", SOL_TOK, 1000, 1.0, "g1", int(clock()))
    e2 = ev("Wb", "buy", SOL_TOK, 1000, 1.0, "g2", int(clock()))
    await eng.on_event(e1)
    await eng.on_event(e2)
    assert any("GROUP BUY" in t for t in note.texts())
    pos = eng.repo.find_open_position(42, "solana", SOL_TOK, "group")
    assert pos is not None
    fills = len(eng.repo.fills(42))

    repo2 = PostgresRepo(pg.url)
    try:
        note2 = MemoryNotifier()
        eng2 = Engine(AppConfig(tz_name="Africa/Lagos"), repo2, ad, note2, None, clock)
        assert any(g.token == SOL_TOK for g in eng2.groups.groups.values())
        await eng2.on_event(e1)
        await eng2.on_event(e2)
        assert note2.texts() == []
        again = repo2.find_open_position(42, "solana", SOL_TOK, "group")
        assert again["id"] == pos["id"]
        assert len(repo2.fills(42)) == fills
    finally:
        repo2.close()


@pytest.mark.asyncio
async def test_morning_report_not_resent_after_restart(pg):
    ts = datetime(2026, 10, 26, 9, 5, tzinfo=ZoneInfo("Africa/Lagos")).timestamp()
    eng, _ad, note, clock = _engines(pg, Clock(ts))
    eng.ensure_user(7, 7)
    await Runner(eng).daily_tick()
    assert any("Morning report" in t for t in note.texts())
    assert pg.kv_get("morning_sent") == "2026-10-26"

    repo2 = PostgresRepo(pg.url)
    try:
        note2 = MemoryNotifier()
        eng2 = Engine(AppConfig(tz_name="Africa/Lagos"), repo2, _ad, note2, None, clock)
        await Runner(eng2).daily_tick()
        assert note2.texts() == []
        assert repo2.kv_get("morning_sent") == "2026-10-26"
    finally:
        repo2.close()
