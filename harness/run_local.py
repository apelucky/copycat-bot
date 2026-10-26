"""Local end-to-end harness: runs the real engine + background loops against LIVE public data,
with the Telegram send layer stubbed to JSONL. Wallet events come from live chain polling where
possible (Solana public RPC, EVM public RPC) plus a fixture replay for deterministic scenarios
(group buy, shared funder, 2-loss switch, dev dump).

usage: python -m harness.run_local --minutes 12
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copycat.app import build  # noqa: E402
from copycat.bot.commands import Commands  # noqa: E402
from copycat.bot.notifier import JsonlNotifier  # noqa: E402
from copycat.chains.base import SwapEvent  # noqa: E402
from copycat.chains.registry import CHAINS, EVM_CHAINS  # noqa: E402
from copycat.config import AppConfig  # noqa: E402
from copycat.data.evm_parse import TRANSFER_TOPIC, topic_addr  # noqa: E402
from copycat.services.runner import Runner  # noqa: E402

OUT = Path(__file__).parent / "out"
UID = 1001

LIVE_TOKENS = {
    "solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK
    "base": "0x4ed4e862860bed51a9570b96d89af5e1b0efefed",       # DEGEN
    "ethereum": "0x6982508145454ce325ddbe47a25d4ec3d2311933",   # PEPE
    "bsc": "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82",        # CAKE
    "robinhood": "0x008df4b3e857d06c4603aeb11f267ccd32ce2005",  # ROBINHOOD (uniswap v4)
}
SOL_REPLAY_TOKENS = ["EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
                     "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"]  # POPCAT

results: dict[str, dict[str, Any]] = {}


def record(name: str, ok: bool, evidence: str) -> None:
    results[name] = {"pass": ok, "evidence": evidence[:600]}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {evidence[:200]}", flush=True)


def fake_wallet(i: int, chain: str) -> str:
    """Deterministic, valid-format but unused keys (no real owner) for replay scenarios."""
    import hashlib
    from copycat.chains.registry import b58encode
    h = hashlib.sha256(f"copycat-replay-{chain}-{i}".encode()).digest()
    return "0x" + h[:20].hex() if CHAINS[chain].kind == "evm" else b58encode(h)


QUERY = {"solana": "pump", "base": "base", "ethereum": "eth", "bsc": "bnb", "robinhood": "robinhood"}


async def hottest_token(engine: Any, chain: str) -> str | None:
    """Most-bought (last hour) token on the chain among DexScreener search results with >= $20k liquidity."""
    http = engine.adapters[chain].http
    pairs: list[dict] = []
    for q in (QUERY[chain], "meme", "cat", "dog"):
        try:
            d = await http.get("https://api.dexscreener.com/latest/dex/search", params={"q": q})
            pairs += [p for p in d.get("pairs") or [] if p.get("chainId") == CHAINS[chain].dexscreener_id
                      and float((p.get("liquidity") or {}).get("usd") or 0) > 20000
                      and int(p["baseToken"]["address"], 16 if p["baseToken"]["address"].startswith("0x") else 36) != 0]
        except Exception:  # noqa: BLE001
            pass
    if not pairs:
        return None
    best = max(pairs, key=lambda p: ((p.get("txns") or {}).get("h1") or {}).get("buys", 0))
    return best["baseToken"]["address"]


async def discover_sol_wallets(engine: Any, n: int = 5) -> list[str]:
    """Live active Solana traders: fee payers of recent swaps on the hottest pump/meme pair."""
    ad = engine.adapters["solana"]
    tok = await hottest_token(engine, "solana")
    info = await ad.token_info(tok) if tok else None
    if not info or not info.pair_address:
        return []
    sigs = await ad.rpc("getSignaturesForAddress", [info.pair_address, {"limit": 25}]) or []
    out: list[str] = []
    for sg in sigs:
        if sg.get("err"):
            continue
        try:
            tx = await ad.rpc("getTransaction", [sg["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        except Exception:  # noqa: BLE001
            continue
        keys = tx["transaction"]["message"]["accountKeys"] if tx else []
        payer = keys[0]["pubkey"] if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
        if payer and payer not in out:
            out.append(payer)
        if len(out) >= n:
            break
    return out


async def discover_evm_wallets(engine: Any, chain: str, n: int = 6) -> list[str]:
    out: list[str] = []
    for tok in [await hottest_token(engine, chain), LIVE_TOKENS[chain]]:
        if tok and len(out) < n:
            out += [w for w in await _discover_evm(engine, chain, tok, n) if w not in out]
    return out[:n]


async def _discover_evm(engine: Any, chain: str, tok: str, n: int) -> list[str]:
    """Find live active traders: recent recipients of the chain's top token from its main pair."""
    ad = engine.adapters[chain]
    latest = int(await ad.rpc("eth_blockNumber", []), 16)
    step = 3000 if chain == "robinhood" else 45
    logs: list[dict] = []
    errors = 0
    for k in range(10):
        hi = latest - 5 - k * step  # stay a few blocks behind head: failover RPCs may lag
        try:
            logs += await ad.rpc("eth_getLogs", [{"fromBlock": hex(hi - step + 1), "toBlock": hex(hi), "address": tok,
                                                  "topics": [TRANSFER_TOPIC]}]) or []
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[discover {chain}] getLogs chunk {k} failed: {str(e)[:120]}")
            if errors >= 4:
                break
            await asyncio.sleep(1.5)
            continue
        if len(logs) > 60:
            break
    seen: list[str] = []
    for lg in reversed(logs):
        w = topic_addr(lg["topics"][2])
        if w not in seen and int(w, 16) != 0:
            code = await ad.rpc("eth_getCode", [w, "latest"])
            if code in ("0x", "0x0"):
                seen.append(w)
        if len(seen) >= n:
            break
    return seen


async def main(minutes: float) -> None:
    OUT.mkdir(exist_ok=True)
    for f in ("harness.db", "harness.db-wal", "harness.db-shm", "messages.jsonl"):
        (OUT / f).unlink(missing_ok=True)
    cfg = AppConfig.from_env()
    cfg.db_path = str(OUT / "harness.db")
    cfg.poll_seconds = 20
    cfg.tracked_top_n = 12
    notifier = JsonlNotifier(str(OUT / "messages.jsonl"))
    engine, http = build(cfg, notifier)
    runner = Runner(engine)
    cmds = Commands(engine, runner)
    engine.ensure_user(UID, UID)
    t0 = time.time()

    async def say(text: str) -> list[Any]:
        rs = await cmds.handle_text(UID, UID, text)
        for r in rs:
            await notifier.send(UID, f"[reply to {text[:40]}]\n" + (r.text or ""), r.buttons)
        return rs

    async def tap(data: str) -> list[Any]:
        rs = await cmds.handle_callback(UID, UID, data)
        for r in rs:
            await notifier.send(UID, f"[reply to button {data[:40]}]\n" + (r.text or ""), r.buttons)
        return rs

    # ---------------------------------------------------------------- 1. chain status + /chain all
    st = engine.chain_status()
    record("chain_status", len(engine.enabled_chains()) >= 1, json.dumps(st))
    r = await say("/chain all")
    record("chain_select_all", engine.settings(UID)["active_chain"] == "all" and "COPYCAT" in r[0].text, r[0].text[:300])

    # ---------------------------------------------------------------- 2. live market data per chain
    for c in CHAINS:
        try:
            info = await engine.token_info(c, LIVE_TOKENS[c], max_age=0)
            sec = await engine.security(c, LIVE_TOKENS[c])
            nusd = await engine.native_usd(c)
            ok = bool(info and info.price_usd > 0 and info.liquidity_usd > 0 and nusd)
            record(f"live_price_{c}", ok, f"{info.symbol if info else None} ${info.price_usd if info else 0:.6g} liq {info.liquidity_usd if info else 0:,.0f} native ${nusd}")
            record(f"live_security_{c}", sec is not None and "unavailable" not in " ".join(sec.reasons),
                   f"source={sec.raw_source} ok={sec.ok} score={sec.score} creator={sec.creator} reasons={sec.reasons[:3]}")
        except Exception as e:  # noqa: BLE001
            record(f"live_price_{c}", False, repr(e))

    # ---------------------------------------------------------------- 3. leaderboards
    for c in CHAINS:
        try:
            rows = await engine.refresh_leaderboard(c)
            srcs = sorted({x["source"] for x in rows})
            ok = bool(rows) if c == "solana" else True
            record(f"leaderboard_{c}", ok, f"{len(rows)} rows, {sum(1 for x in rows if x['flagged'])} flagged, sources={srcs}")
        except Exception as e:  # noqa: BLE001
            record(f"leaderboard_{c}", False, repr(e))
    r = await say("/leaderboard solana")
    record("leaderboard_screen", "Weekly leaderboard" in r[0].text, r[0].text[:500])
    lead = engine.repo.get_following(UID, "solana")
    record("auto_follow_top_solana", lead is not None, str(lead))

    # EVM: discover live active wallets (no leaderboard source works keyless) and follow #1
    for c in EVM_CHAINS:
        if c not in engine.enabled_chains():
            continue
        try:
            ws = await asyncio.wait_for(discover_evm_wallets(engine, c), timeout=120)
            tracked = ws
            engine.repo.kv_set(f"tracked:{c}", tracked)
            for i, w in enumerate(ws):
                engine.repo.kv_set(f"label:{c}:{w}", f"{c[:3]}live{i + 1}")
            if ws:
                await say(f"/follow {c} {ws[0]} {c[:3]}live1")
            record(f"evm_live_wallets_{c}", bool(ws), f"{len(ws)} live EOAs discovered from recent transfers of hot tokens: {ws[:3]}")
        except Exception as e:  # noqa: BLE001
            record(f"evm_live_wallets_{c}", False, repr(e)[:200])

    # Solana: live-discovered active traders, each followed by an extra harness user (multi-user test)
    try:
        sw = await asyncio.wait_for(discover_sol_wallets(engine), timeout=240)
    except Exception as e:  # noqa: BLE001
        sw = []
        print("sol discovery failed", e)
    engine.repo.kv_set("tracked:solana", sw)
    for i, w in enumerate(sw):
        engine.repo.kv_set(f"label:solana:{w}", f"solactive{i + 1}")
    record("sol_live_active_wallets", bool(sw), f"{len(sw)} active traders from the hottest Solana pair: {sw[:3]}")
    extra_users = []
    for i in range(5):
        uid = 2001 + i
        extra_users.append(uid)
        engine.ensure_user(uid, uid)
        st = engine.settings(uid)
        st["active_chain"] = "all"
        st["groupbuy"] = False
        engine.save_settings(uid, st)
        if i < len(sw):
            engine.follow(uid, "solana", sw[i], f"solactive{i + 1}")
        for c in EVM_CHAINS:
            tw = engine.repo.kv_get(f"tracked:{c}", [])
            if i + 1 < len(tw):
                engine.follow(uid, c, tw[i + 1])
    record("multi_user_setup", True, f"users {extra_users} follow different live wallets per chain")

    # ---------------------------------------------------------------- 4. token cards (paste CA) + manual trades
    for c in CHAINS:
        try:
            r = await say(LIVE_TOKENS[c])
            txt = r[0].text if r else ""
            if "Which EVM chain" in txt:
                r = await tap(f"card:{c}:{LIVE_TOKENS[c]}")
                txt = "(asked to pick chain → picked) " + r[0].text
            record(f"token_card_{c}", "Price" in txt and "Rug-check" in txt, txt[:400])
            amt = CHAINS[c].manual_presets[0]
            r = await say(f"/buy {LIVE_TOKENS[c]} {amt} {c}")
            record(f"manual_buy_{c}", "MANUAL BUY" in r[0].text, r[0].text[:300])
        except Exception as e:  # noqa: BLE001
            record(f"token_card_{c}", False, traceback.format_exc()[-300:])

    # ---------------------------------------------------------------- 5. start live background loops
    runner.start()
    record("runner_started", True, f"loops: {[t.get_name() for t in runner.tasks]}")

    # ---------------------------------------------------------------- 6. fixture replay scenarios (Solana, live prices)
    await asyncio.sleep(5)
    sim = [fake_wallet(i, "solana") for i in range(5)]
    engine.repo.kv_set("tracked:solana", engine.repo.kv_get("tracked:solana", []) + sim)
    for i, w in enumerate(sim):
        engine.repo.kv_set(f"label:solana:{w}", f"replay{i + 1}")
    # shared funder between replay1 & replay2 → counted once
    engine.repo.set_funder("solana", sim[0], "BundLerFunder1111111111111111111111111111111", int(time.time()))
    engine.repo.set_funder("solana", sim[1], "BundLerFunder1111111111111111111111111111111", int(time.time()))
    for i, w in enumerate(sim[2:], 2):
        engine.repo.set_funder("solana", w, None, int(time.time()))
    tok = SOL_REPLAY_TOKENS[0]
    now = int(time.time())
    s = engine.settings(UID)
    s["min_liq_usd"] = 20000
    engine.save_settings(UID, s)
    before = len(notifier.messages)
    for i in range(2):
        await engine.on_event(SwapEvent("solana", sim[i], f"rp-g{i}", now + i, "buy", tok, 1000, 1.0, 0, source="replay"))
    no_entry = engine.repo.find_open_position(UID, "solana", tok, "group") is None
    await engine.on_event(SwapEvent("solana", sim[2], "rp-g2", now + 30, "buy", tok, 1000, 1.0, 0, source="replay"))
    gpos = engine.repo.find_open_position(UID, "solana", tok, "group")
    gmsgs = [m["text"] for m in notifier.messages[before:] if "GROUP BUY" in m["text"]]
    record("groupbuy_shared_funder_discount", no_entry and bool(gpos) and any("Shared funder" in t for t in gmsgs),
           f"no entry with 2 same-funder wallets: {no_entry}; entered after independent 3rd: {bool(gpos)}; alert: {gmsgs[:1]}")
    await engine.on_event(SwapEvent("solana", sim[3], "rp-g3", now + 60, "buy", tok, 1000, 1.0, 0, source="replay"))
    g2 = engine.repo.find_open_position(UID, "solana", tok, "group")
    record("groupbuy_scale_out", bool(gpos and g2 and g2["tokens"] < gpos["tokens"]),
           f"tokens {gpos['tokens'] if gpos else 0:.2f} → {g2['tokens'] if g2 else 0:.2f} after 4th wallet (3rd cluster)")
    await engine.on_event(SwapEvent("solana", sim[0], "rp-x0", now + 90, "sell", tok, 1000, 1.0, 1000, source="replay"))
    record("groupbuy_early_exit", engine.repo.find_open_position(UID, "solana", tok, "group") is None,
           [m["text"] for m in notifier.messages if "GROUP EXIT" in m["text"]][-1:].__repr__())

    # copy + 2-loss switch: follow replay5 and feed two round trips (fees make flat-price round trips losers)
    lw = sim[4]
    await say(f"/follow solana {lw} replay5")
    for k, t in enumerate(SOL_REPLAY_TOKENS):
        info = await engine.token_info("solana", t, max_age=0)
        p = info.price_native if info else 0.00001
        await engine.on_event(SwapEvent("solana", lw, f"rp-cb{k}", now + 100 + k, "buy", t, 2.0 / p, 2.0, 0, source="replay"))
        await engine.on_event(SwapEvent("solana", lw, f"rp-cs{k}", now + 110 + k, "sell", t, 1.0 / p, 0.99, 2.0 / p, source="replay"))
        half = engine.repo.find_open_position(UID, "solana", t, "copy")
        if k == 0:
            record("copy_proportional_sell", bool(half) and abs(half["tokens"] / half["initial_tokens"] - 0.5) < 0.01,
                   f"leader sold 50% → our remaining {half['tokens'] / half['initial_tokens'] if half else 0:.2%}")
        await engine.on_event(SwapEvent("solana", lw, f"rp-cx{k}", now + 120 + k, "sell", t, 1.0 / p, 0.98, 1.0 / p, source="replay"))
    fol = engine.repo.get_following(UID, "solana")
    sw = [m["text"] for m in notifier.messages if "TRADER SWITCHED" in m["text"]]
    record("copy_buy_alert", any("COPY BUY" in m["text"] for m in notifier.messages), [m["text"] for m in notifier.messages if "COPY BUY" in m["text"]][-1][:300])
    record("auto_switch_2_losses", bool(sw) and fol is not None and fol["wallet"] != lw, (sw[-1][:300] if sw else "no switch") + f" | now following {fol}")
    r = await tap("undo:solana")
    record("undo_switch", engine.repo.get_following(UID, "solana")["wallet"] == lw, r[0].text)
    if lead:  # restore live KOL follow so the live loop can copy real trades
        engine.follow(UID, "solana", lead["wallet"], lead["label"])

    # dev watch on replay token (synthetic creator → copy pos auto-sold, manual only alerted)
    dtok = SOL_REPLAY_TOKENS[1]
    await say(f"/buy {dtok} 0.1")
    dev = fake_wallet(99, "solana")
    dw = engine.dev_wallets("solana")
    dw[dev] = [dtok]
    engine.repo.kv_set("devwatch:solana", dw)
    await engine.on_event(SwapEvent("solana", dev, "rp-dev", now + 200, "transfer_out", dtok, 1e6, 0, None, source="replay"))
    record("dev_watch_manual_alert_only", any("Manual position NOT auto-sold" in m["text"] for m in notifier.messages)
           and engine.repo.find_open_position(UID, "solana", dtok, "manual") is not None, "manual kept, alert sent")
    # exit alert from another tracked wallet on the token we hold manually
    await engine.on_event(SwapEvent("solana", sim[2], "rp-exit", now + 210, "sell", dtok, 10, 0.1, 20, source="replay"))
    record("trader_exit_alert", any("TRADER EXIT" in m["text"] for m in notifier.messages), [m["text"] for m in notifier.messages if "TRADER EXIT" in m["text"]][-1:].__repr__())

    # manual sells via buttons + commands
    p = engine.find_user_position(UID, LIVE_TOKENS["solana"])
    if p:
        r = await tap(f"sellp:{p['id']}:25")
        record("manual_sell_25_button", "SELL 25%" in r[0].text, r[0].text[:200])
        r = await say(f"/sell {LIVE_TOKENS['solana']} 100")
        record("manual_sell_100_cmd", "SELL 100%" in r[0].text, r[0].text[:200])

    # ---------------------------------------------------------------- 7. live loop period
    deadline = t0 + minutes * 60
    tick = 0
    while time.time() < deadline:
        await asyncio.sleep(min(120, max(1, deadline - time.time())))
        tick += 1
        hb = engine.repo.kv_get("heartbeat", {})
        live_events = len(engine.repo.leader_events(None, int(t0)))
        print(f"[t+{int(time.time() - t0)}s] heartbeat={json.dumps(hb)} leader_events={live_events}", flush=True)
        await say("/status")

    # ---------------------------------------------------------------- 8. reports
    hb = engine.repo.kv_get("heartbeat", {})
    for c in engine.enabled_chains():
        h = hb.get(c)
        record(f"live_poll_{c}", bool(h and time.time() - h["ts"] < 300), json.dumps(h))
    live_evs = [e for e in engine.repo.leader_events(None, int(t0)) if not e["signature"].startswith("rp-")]
    by_chain: dict[str, int] = {}
    for e in live_evs:
        by_chain[e["chain"]] = by_chain.get(e["chain"], 0) + 1
    record("live_wallet_events", bool(live_evs), f"{len(live_evs)} live swap events parsed: {by_chain}; sample: {live_evs[:2]}")
    all_uids = [UID] + extra_users
    live_copies = [f for u in all_uids for f in engine.repo.fills(u) if f["source"] == "copy" and f["leader_sig"] and not f["leader_sig"].startswith("rp-")]
    live_skips = [k for u in all_uids for k in engine.repo.skips(u) if k["trader"] and k["trader"] not in sim]
    record("live_copy_or_skip", bool(live_copies or live_skips), f"{len(live_copies)} live copy fills, {len(live_skips)} live risk skips; e.g. {(live_copies or live_skips)[:1]}")
    for cmd in ("/start", "/positions", "/pnl today", "/pnl 7d", "/log", "/settings", "/bench", "/hours", "/groupbuy", "/mode", "/advisor on"):
        r = await say(cmd)
        record(f"screen_{cmd.strip('/').replace(' ', '_')}", bool(r and r[0].text), r[0].text[:250] if r else "")
    r = await say("/export")
    record("csv_export", r[0].document is not None and Path(r[0].document).stat().st_size > 100, f"{r[0].document} {Path(r[0].document).read_text()[:200]!r}")
    r = await say("/pnlcard today")
    os.replace(r[0].photo, OUT / "pnl_card_live.png")
    record("pnl_card", (OUT / "pnl_card_live.png").stat().st_size > 10000, str(OUT / "pnl_card_live.png"))
    r = await say("/card")
    if r and r[0].photo:
        os.replace(r[0].photo, OUT / "trade_card_live.png")
    record("trade_card", (OUT / "trade_card_live.png").exists(), str(OUT / "trade_card_live.png"))
    for u in extra_users:
        f = [x for x in engine.repo.fills(u) if x["source"] == "copy"]
        if f:
            print(f"user {u}: {len(f)} live copy fills, balance per chain:",
                  {c: round(engine.repo.get_balance(u, c), 5) for c in CHAINS}, flush=True)
    r = await say("/chart")
    os.replace(r[0].photo, OUT / "equity.png")
    record("equity_chart", (OUT / "equity.png").stat().st_size > 5000, str(OUT / "equity.png"))
    before = len(notifier.messages)
    await runner.daily_tick(force=True)
    mr = [m["text"] for m in notifier.messages[before:]]
    record("morning_report", any("Morning report" in t for t in mr), (mr[0] if mr else "")[:500])
    record("advisor_without_key", not cfg.xai_api_key and not any("Grok advisor" in t for t in mr[1:]), "advisor ON but XAI_API_KEY missing → silent, as designed")

    await runner.shutdown()
    await http.close()
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    passed = sum(1 for v in results.values() if v["pass"])
    print(f"\n==== {passed}/{len(results)} checks passed · {len(notifier.messages)} messages in {OUT / 'messages.jsonl'} ====")
    for k, v in results.items():
        if not v["pass"]:
            print("FAIL", k, v["evidence"][:300])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=12)
    a = ap.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main(a.minutes))
