# 🐱 Copycat Copy-Trader (PAPER)

Multi-chain memecoin **copy-trading Telegram bot — paper trading only**. It follows top wallets,
simulates copying their trades with realistic fills (price impact, DEX fee, gas), and reports PnL.
**No private keys, no real swaps.** LIVE mode is a locked placeholder.

Chains: **Solana** (primary, full features), **Base**, **Ethereum**, **BNB Chain**, **Robinhood Chain** (EVM, shared adapter).

## Features (v1)

| Area | What it does |
|---|---|
| Trader discovery | Kolscan weekly leaderboard (Solana, live), GMGN (attempted, usually Cloudflare-blocked → graceful fallback), seed list `seeds/wallets.json`, manual `/follow`. Filters bots (huge trade counts), bait (~100% win rate / tiny samples), wash patterns, <30 trades, negative PnL. Rank score = PnL × win rate × trade-count confidence. Auto-follows top non-benched wallet; refreshes every 6 h. |
| Copy engine | Watches leader wallets; copies buys at **0.25x** (configurable) capped at max position; mirrors partial sells proportionally (leader sells 50% → we sell 50%). Logs leader fill vs our fill and slippage. Simulated fills: constant-product price impact from DexScreener liquidity + 0.3% latency drag + DEX fee + per-chain gas. |
| Auto-switch | Consecutive-loss counter per trader; after **2** losses (configurable) → bench **24 h**, follow next ranked; alert with **Undo** button. |
| Trader health | Rolling last-20 copied trades (60% win rate + 40% avg return); demotes cold traders (≥8 copies, health < 0.35) before the 2-loss rule. |
| Copy-delay tracker | Per-trader average entry cost vs leader; stops copying a trader whose last 5–10 entries cost > 4% (configurable). |
| Group Buy | Watches all tracked wallets; ≥2 distinct wallets buying the same token within 10 min → alert (who, size, order). Paper-enters on the 2nd buy, scales out 25% as the 3rd/4th/5th wallets enter (or on an X mention spike), full exit if an earliest buyer sells. Wallets with the **same funding wallet** count as one cluster. |
| X mention check | X API v2 recent search for `$TICKER` and the contract address: count, weighted score (older/bigger/verified accounts weigh more, new/tiny/bot-like less), 15-min velocity vs 2-h baseline, notable accounts. Spikes feed Group Buy scale-out. Disabled without `X_BEARER_TOKEN`. |
| Exit alerts | Any tracked wallet selling a token you hold → warning with Sell 50%/100% buttons. |
| Dev wallet watch | Creator of each held token (RugCheck / GoPlus / pump.fun) is added to the watch list; if the dev sells or transfers out → immediate paper sell (copy/group positions). |
| Manual paper trading | `/buy <CA> <amount>`, `/sell <token|CA> <pct>`; paste a contract address → token card (price, mcap, liquidity, rug-check, tracked holders, X mentions) with Buy presets / Custom and Sell 25/50/100% on held positions. Same fills, fees, risk caps and PnL; tagged `manual`. Copy logic never auto-sells manual trades; dev-watch/exit **alerts** on by default, **auto-sell** opt-in (`manual_auto_sell`). |
| Risk controls | Daily loss cap → auto-pause per chain until midnight WAT; max position; min liquidity ($20k default); rug-check skip; max slippage (3%); global pause; skipped-trade log with reasons; optional pause during your worst hours. |
| Reporting | `/pnl today|7d|30d` (win rate, avg win/loss, best/worst, avg slippage vs leader, fees+gas, skips, switches, per-trader incl. `manual` and `group buy`), `/log`, `/export` CSV, `/chart` equity PNG, shareable **trade cards** (🖼 button on every closed trade, `/card <id>`) and **`/pnlcard today|7d|30d`** PNG summary card, `/hours` time-of-day stats (WAT), **9 AM WAT morning report**. |
| Multi-chain | `/chain` (or buttons) picks Solana/Base/Ethereum/BSC/Robinhood or **All**; per-chain paper balances (10 SOL, 0.5 ETH Base/Ethereum/Robinhood, 2 BNB), positions, PnL, followed trader, max position / loss cap / group size. Alerts carry a chain badge. 0x addresses auto-detected via DexScreener; ambiguous → chain picker. |
| Grok advisor (optional) | `XAI_API_KEY`, OFF by default. Daily trader review + plain summary with the morning report. Advisory only; never trades. Input is numeric stats + sanitized labels inside an explicit "untrusted data" block; its output is shown as text and never parsed into actions. |
| Access | `ALLOWED_USER_IDS` gate; each user gets their own paper balances. |

## Data sources (verified from a server on 2026-10-26)

| Source | Key? | Status |
|---|---|---|
| DexScreener (all 5 chains, incl. `robinhood`) | none | ✅ works |
| RugCheck (Solana) | none | ✅ works (summary + creator) |
| GoPlus Security (Base 8453, ETH 1, BSC 56, Robinhood 4663) | none | ✅ works |
| Kolscan leaderboard (Solana) | none | ✅ works (parsed from the public page payload; may break if they change the site) |
| GMGN leaderboards | — | ⚠️ Cloudflare 403 from servers → falls back to seeds / `/follow` |
| pump.fun frontend API | — | ⚠️ Cloudflare 403 from servers → RugCheck creator used instead |
| Solana public RPC | none | ✅ works but rate-limited; **set `HELIUS_API_KEY`** for reliable streaming |
| Helius enhanced transactions | `HELIUS_API_KEY` | implemented, fixture-tested (no key on build box) |
| EVM public RPCs (drpc / 1rpc / Robinhood official) | none | ✅ work with 50-block `eth_getLogs` chunks; set `RPC_URL_<CHAIN>` (Alchemy/QuickNode) for production |
| Etherscan v2 / Blockscout (funding source) | `ETHERSCAN_API_KEY` optional | eth.blockscout.com works keyless; others need the key (Etherscan free tier may not cover Base/BSC) |
| X API v2 recent search | `X_BEARER_TOKEN` | implemented, fixture-tested; disabled without key |
| xAI Grok | `XAI_API_KEY` | implemented; disabled without key |

**Robinhood Chain**: Arbitrum Orbit L2, mainnet since 2026-07-01, chain id **4663**, gas in ETH, public RPC
`https://rpc.mainnet.chain.robinhood.com`, explorer `robinhoodchain.blockscout.com`. DexScreener and GoPlus cover
it (Uniswap v3/v4 pools + launchpads such as flap.sh). Missing: no GMGN/Kolscan leaderboard → use the seed list or
`/follow`. Native-ETH v4 swaps are detected via tx value; WETH address is learned from DexScreener pairs.

## Local setup

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill TELEGRAM_BOT_TOKEN + ALLOWED_USER_IDS at least
pytest -q
set -a; . ./.env; set +a; python -m copycat
```

Harness without Telegram (live public data + replay scenarios, messages written to `harness/out/messages.jsonl`):

```bash
python -m harness.run_local --minutes 12
```

## Create the Telegram bot (BotFather)

1. In Telegram open **@BotFather** → `/newbot` → name `Copycat Copy-Trader`, username e.g. `copycat_paper_bot`.
2. Copy the token → `TELEGRAM_BOT_TOKEN`.
3. Optional: `/setdescription`, `/setuserpic`. Commands are registered automatically at start-up.
4. Get your numeric user id (e.g. message **@userinfobot**) → `ALLOWED_USER_IDS=123456789` (comma-separate several). **Leave empty only for testing — empty means anyone can use it.**

## Deploy on Render (free)

The free plan only runs a **web service**, sleeps after 15 minutes without visitors, and forgets local files. This repo listens on `$PORT` (`/` and `/health`) and, when `DATABASE_URL` is set, stores every balance, position, bench timer, and alert marker in Postgres (Supabase). Step-by-step for a non-developer: **[DEPLOY_RENDER.md](DEPLOY_RENDER.md)**.

| Setting | Value |
|---|---|
| Service type | Web Service, **Free** |
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m copycat` |
| Health check path | `/health` |
| Required env | `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `DATABASE_URL`, `TZ=Africa/Lagos` |

Keep one instance only (Telegram polling). A free monitor should request `/health` every 10 minutes so Render does not spin the service down. Without `DATABASE_URL` and without `PORT`, a laptop run is unchanged: SQLite file, no web port.

Postgres tests use a local server at `postgresql:///copycat_test` (or `TEST_DATABASE_URL`). They skip if that server is not running. `pytest -q` still runs the SQLite suite.

## Architecture

```
copycat/
  config.py            env config
  app.py               wiring (adapters, repo, engine)
  chains/              ChainAdapter interface, registry (5 chains), solana.py, evm.py
  data/                http (rate limit + backoff), dexscreener, rugcheck, goplus, leaderboards (kolscan/gmgn/seeds),
                       pumpfun, solana_parse, evm_parse, xsearch
  core/                engine (copy/switch/group/dev/exit/manual), fills, sizing, ranking/health, groupbuy, risk,
                       stats, reports, settings, advisor
  storage/             repo.py (protocol), sqlite_repo.py (local default), postgres_repo.py (DATABASE_URL / Supabase)
  bot/                 commands.py (transport-independent), telegram_app.py (PTB 21), ui.py, notifier.py (Telegram/JSONL)
  services/runner.py   wallet polling per chain, leaderboard refresh, 9 AM report, advisor
harness/run_local.py   live end-to-end harness without Telegram
tests/                 pytest (fixtures captured from live APIs)
```

`DATABASE_URL` selects Postgres (`storage/postgres_repo.py`, Supabase pooler + SSL). Unset, the bot uses SQLite.

## Known limitations

- Default min liquidity is **$20k** (the mockup showed $50k); change with `/set min_liq_usd 50000`.
- Live E2E harness: `python -m harness.run_local --minutes 13` (JSONL output in `harness/out/`, no Telegram).

- Wallet monitoring is polling (Solana per-wallet signatures; EVM `eth_getLogs` on ERC-20 Transfer events), 20–30 s cadence. Copies are paper, so latency only affects the simulated fill price (which uses the live DexScreener price at copy time). On Render's free plan the process is paused while the service is spun down; leader trades during a long sleep can be missed. A restart does not repeat alerts or open the same paper position twice (events are deduped in the database).
- EVM sells into **native ETH/BNB** settle via internal transfers that logs don't show; proceeds are then estimated from price. Plain token transfers out are treated like sells (conservative for copy/dev-watch).
- Funding-source lookups: Solana scans up to 3k signatures back (may miss the true first funder of very old wallets); EVM needs Etherscan v2 key or Blockscout.
- Simulated fills ignore MEV/sandwich and bonding-curve specifics (pump.fun pre-migration).
- GMGN and pump.fun block server IPs; Kolscan is parsed from page data and can break on site changes.
