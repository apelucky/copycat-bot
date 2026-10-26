# Put Copycat on Render for free

Copycat is paper trading only. It never asks for a wallet private key or seed phrase, and it never sends a real swap. Do not add either of those to Render.

Render's free plan only runs a **web service**. It sleeps after 15 minutes with no visitors, and it forgets files on the machine (so a SQLite file would be wiped). This build listens for a simple health check on the address Render gives you, and it stores the bot's memory in a free Supabase Postgres database. A free monitor visits that health address every 10 minutes so the bot stays awake. One always-on service fits in the 750 free hours each month.

The bot still **polls** Telegram, the same way it does on a laptop. You do not set up a Telegram webhook. Polling needs no public address of its own, and it keeps working after a restart. The public address is only for the health check.

## What you need

- The GitHub repo `apelucky/copycat-bot` (this project).
- A Telegram bot token from **@BotFather**, and your numeric Telegram user id (message **@userinfobot**).
- A free account on [supabase.com](https://supabase.com).
- A free account on [render.com](https://render.com).
- A free account on [uptimerobot.com](https://uptimerobot.com) or [cron-job.org](https://cron-job.org).

## 1. Create the Supabase database

1. Sign up at [supabase.com](https://supabase.com) and create a new project. Save the database password somewhere safe (a notes app is fine). Wait until the project says it is ready.
2. In the project, click **Connect**.
3. Copy the **Session pooler** URI, or the **Transaction pooler** URI if you prefer that. Both work.
   - The host must contain `pooler.supabase.com`.
   - Do **not** use the direct connection (`db.something.supabase.co`). On the free Supabase plan that address is IPv6 only, and Render often cannot reach it.
4. Replace `[YOUR-PASSWORD]` with the database password.
5. If the password contains symbols such as `@`, `:`, `/`, `#`, or `?`, the URI will break. In Supabase go to **Project Settings → Database** and reset the password to letters and numbers only, then paste it again.

The URI looks like this (yours will differ):

```text
postgresql://postgres.abcdefghijklmnop:YOUR_PASSWORD@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
```

You do not have to add `sslmode` yourself. If it is missing, the bot turns SSL on for any database that is not on your own computer. It also turns off prepared statements so the transaction pooler (port **6543**) accepts the connection. Leave the URI as one line. Do not commit it to GitHub.

## 2. Create the Render web service

**Blueprint (simplest)**

1. In the Render dashboard click **New → Blueprint**.
2. Connect GitHub and choose `apelucky/copycat-bot`.
3. Render reads `render.yaml` from the repo. Accept it.
4. When Render asks for the secret values, paste them (see the next section). You can leave the optional ones empty.

**Or create the service by hand**

1. **New → Web Service**. Connect the same GitHub repo.
2. Use these settings exactly:

| Setting | Value |
|---|---|
| Service type | Web Service |
| Plan / instance type | Free |
| Language | Python |
| Python version | 3.11 (the repo's `.python-version` file) |
| Region | Frankfurt is set in the blueprint (closer to West Africa). You can pick the region closest to your Supabase project instead. |
| Branch | `main` |
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m copycat` |
| Health check path | `/health` |

Do **not** type a `PORT` variable. Render sets that itself. Do **not** add a disk. Do **not** create a Render Postgres database (the free one is deleted after 30 days).

## 3. Environment variables

In the Render service, open **Environment** and set:

| Name | Required? | Value |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Token from BotFather |
| `ALLOWED_USER_IDS` | Yes | Your numeric Telegram id. Several ids: `111,222`. Empty means anyone on the internet can use the bot. |
| `DATABASE_URL` | Yes | The Supabase pooler URI from step 1 |
| `TZ` | Yes | `Africa/Lagos` |
| `HELIUS_API_KEY` | Recommended | Solana wallet reads. Without it the public RPC is slow and rate-limited. |
| `RPC_URL_SOLANA` | Optional | Your own Solana RPC, if you have one |
| `RPC_URL_BASE` | Optional | Base RPC |
| `RPC_URL_ETHEREUM` | Optional | Ethereum RPC |
| `RPC_URL_BSC` | Optional | BNB Chain RPC |
| `RPC_URL_ROBINHOOD` | Optional | Robinhood Chain RPC |
| `ETHERSCAN_API_KEY` | Optional | Funding-source lookups on EVM chains |
| `X_BEARER_TOKEN` | Optional | X/Twitter mention checks. Off if empty. |
| `XAI_API_KEY` | Optional | Grok advisor. Off if empty. Advisory only; it cannot trade. |
| `XAI_MODEL` | Optional | `grok-4` (already the default) |

`PYTHONUNBUFFERED=1` is set by the blueprint so logs show up immediately. You can ignore it.

Click **Deploy** (or **Save**) and wait until the status is **Live**. The log should mention the health server and Telegram polling. It must not print your database password.

## 4. Check the health address

Open `https://YOUR-SERVICE.onrender.com/health` in a browser (the name is on the Render service page). You should see a short message like:

```json
{"ok":true,"service":"copycat","mode":"paper","db":"postgres","db_ok":true}
```

`/` shows the same thing. `mode` is always `paper`.

Then open your bot in Telegram and send `/start`, then `/chain`.

## 5. Keep it awake

Render sleeps a free web service after **15 minutes** with no visitors. Sleeping does not use the 750 hours, but the bot also stops watching wallets. A monitor that visits `/health` every 10 minutes counts as a visitor.

**UptimeRobot**

1. Add a new monitor.
2. Type: HTTP(s).
3. URL: `https://YOUR-SERVICE.onrender.com/health`
4. Interval: 5 minutes if that is the free minimum, otherwise 10 minutes.

**cron-job.org**

1. Create a cron job.
2. URL: `https://YOUR-SERVICE.onrender.com/health`
3. Schedule: every 10 minutes.

When the monitor is working, the Render logs show a request to `/health` about that often, and the service stays **Live**.

If the monitor stops, the bot sleeps. Waking takes about a minute. Leader trades that happen during a long sleep can be missed. When it wakes it does **not** repeat old alerts and does **not** open the same paper position a second time. Messages you sent Telegram while it was asleep are dropped; send them again.

## 6. Rules that keep it free and safe

- One Render service only. A second copy (your laptop, an old Railway deploy, another Render service) using the same bot token will fight with this one and Telegram will reject both.
- Leave pull-request previews off. A preview would be a second copy of the bot.
- Paper only. Never paste a private key, seed phrase, or a "live trading" key into the environment.
- The database password and the bot token stay in Render and Supabase only.

## If it does not work

- **Deploy failed or `/health` never loads.** Open the Render logs. The usual causes are a missing `TELEGRAM_BOT_TOKEN`, or a `DATABASE_URL` that still says `[YOUR-PASSWORD]`, uses the direct `db.*.supabase.co` host, or has a symbol in the password. Fix the variable and deploy again.
- **`/health` loads but `"db_ok": false`.** The database connection dropped. Check the Supabase project is not paused, and that the URI is the pooler one.
- **The bot does not reply in Telegram.** Your user id must be in `ALLOWED_USER_IDS`. Get it from **@userinfobot**. Also confirm no other program is running the same bot token.
- **Balances reset.** `DATABASE_URL` was empty, so the bot used a file on Render's disk and that file was wiped. Set `DATABASE_URL` and deploy again. Old paper balances from before that are gone.

## Running on your own computer

Leave `DATABASE_URL` empty and do not set `PORT`. Then `python -m copycat` uses the SQLite file (`DB_PATH`, default `copycat.db`) and does not open a web port. Setting `DATABASE_URL` locally talks to that Postgres database instead, which is useful if you want to inspect the same data.
