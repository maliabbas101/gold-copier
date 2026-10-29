# Gold Community Copier — v0.1

A working software MVP for copying one authorized trader's Discord XAUUSD signals to multiple independent MT5 **demo** accounts. This project includes a Discord listener, an authenticated queue API, a Windows MT5 worker, durable execution journals, and automated tests. It has not been exercised against a real MT5 terminal in this environment. Live account support is deliberately disabled pending demo acceptance and production hardening.

## Architecture

Discord bot → shared SQLite queue ← HTTPS polling workers → dedicated MT5 terminals.

Run API and Discord listener on the same machine against the same local SQLite file. Each consenting follower runs a worker on Windows beside a separate MT5 terminal already logged into their demo account. Credentials remain in MT5. A worker token can claim only that follower's jobs. Exness and PrimeXBT **MT5** accounts use this adapter; PrimeXBT PXTrader and MT4 are not implemented.

## Exact signal format

```text
XAUUSD BUY
ENTRY: 2700
SL: 2695
TP: 2710
BE: 2705
```

For a sell, require `TP < BE < ENTRY < SL`. For a buy, require `SL < ENTRY < BE < TP`. BE means a **price trigger**: once the executable exit quote reaches it, move SL to that position's actual entry. This is price breakeven, excluding commission, swap, and slippage.

One market entry, one TP, one stop loss, one BE trigger. ENTRY is a reference price; the worker executes at market only within its configured tolerance. There are no pending entries or delayed waits for price. Price ranges, multiple targets, images, embeds, edits, partial closes, cancellations from Discord, and free-form “move BE” messages are rejected or ignored. Adapt the strict parser only after collecting actual trader examples and expected behavior.

## 1. Install and test

Python 3.11+:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.lock.txt
pip install -e . --no-deps
pytest -q
python -m goldcopier.demo
```

`requirements.lock.txt` records the versions tested here. MetaTrader5 is separate and Windows-only: on the worker run `pip install '.[mt5]'`. The offline demo uses a simulated broker, not live market data or an MT5 connection.

## 2. Configure the service

Copy `config.example.json` to `config.json`. Set guild, channel, and trader IDs from Discord Developer Mode. Every follower gets a different high-entropy token. Generate an admin token and each agent token separately:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Store raw tokens only in local environment variables or your secret manager. Hash each agent token with SHA256 and put only its 64-character hex digest in `config.json` under a unique follower ID. To hash securely without terminal history, use:

```bash
python -c "import getpass,hashlib; print(hashlib.sha256(getpass.getpass('Agent token: ').encode()).hexdigest())"
```

Set `COPIER_ADMIN_TOKEN`, `DISCORD_BOT_TOKEN`, optional `COPIER_CONFIG`, and optional `COPIER_DB` in the service's environment. `.env` files are not automatically loaded. Never commit tokens, account settings, databases, or terminal passwords.

Create a Discord bot, add it to the server with permission to view the signal channel, and enable its privileged **Message Content Intent** in the Developer Portal. Additional Discord approval may apply to verified bots. The bot only listens; it does not post messages or fetch old signals. It accepts only the configured trader, guild, and channel and accepts either human or bot authors matching those IDs. It ignores webhooks and its own messages.

Start two processes from the project folder with the same working directory/config/database:

```bash
uvicorn goldcopier.server:create_app --factory --host 127.0.0.1 --port 8000
python -m goldcopier.discord_bot
```

Expose API access through a maintained HTTPS reverse proxy with request-size limits, rate limits, and access controls; do not expose plain HTTP externally. SQLite must be on local storage, not a network share. Keep both service processes single-host. The API starts **paused** on a new database. Pause state persists across restarts.

Admin API (all require `Authorization: Bearer <admin token>`):

| Endpoint | Purpose |
| --- | --- |
| `GET /admin/status` | Pause status and latest 100 follower jobs/results |
| `POST /admin/preview` | Parse `{"text":"..."}` without queueing or trading |
| `POST /admin/pause` | `{"paused":false}` enables intake/claims; `true` cancels queued jobs |

For example, in PowerShell with `COPIER_ADMIN_TOKEN` set:

```powershell
$headers = @{Authorization = "Bearer $env:COPIER_ADMIN_TOKEN"}
Invoke-RestMethod http://127.0.0.1:8000/admin/status -Headers $headers
Invoke-RestMethod http://127.0.0.1:8000/admin/pause -Method Post -Headers $headers -ContentType 'application/json' -Body '{"paused":false}'
```

## 3. Configure each MT5 demo worker

1. Install and log into a **dedicated hedging demo account** in its own Windows MT5 terminal installation. Enable algorithmic trading and external Python trading. Keep the terminal running.
2. Copy `agent.example.json` to `agent.json`. Set the exact terminal executable, expected login/server, and exact gold symbol (e.g. broker suffix `XAUUSDm`). Only map the broker's actual XAUUSD contract here.
3. Set follower-specific risk percent, max lots, spread, entry-distance, and broker-supported filling mode. Example settings are illustrative, not recommended trading parameters. `max_spread` and `max_entry_distance` are gold **price units**, while `deviation_points` uses the broker's point size. `filling_mode` is an MT5 enum: 0 FOK, 1 IOC, 2 RETURN; verify broker support. Unsupported modes are rejected by preflight.
4. Set `COPIER_URL` to the HTTPS API URL and `AGENT_TOKEN` to that follower's raw token.
5. Start `python -m goldcopier.agent`. Use a separate working directory, journal, token, and terminal for every follower. Never run two workers for one account, including across different machines. The lock file only protects one local journal.

No account password is sent to the service. The worker verifies account login/server, demo mode, hedging mode, trade permissions, quote freshness, broker preflight, and account exposure before entry. Existing positions or pending orders anywhere on the dedicated account block another trade. Initial position size uses `order_calc_profit` in account currency, equity risk, and broker lot step/min/max; it rounds down or skips a trade below the minimum lot. Risk percent is capped at 1% in v0.1. Stops and slippage do not guarantee a maximum realized loss.

## Pause and failure behavior

- Central pause cancels queued jobs and stops future intake/claims. A previously claimed or already submitted order can still execute. Pause does not close open positions.
- Create a local file named `PAUSE` in a worker's working directory to stop new entries there. Breakeven monitoring continues.
- SL and TP are sent with the opening order and managed by the broker after acceptance. BE requires the local worker/terminal and fresh quotes to remain available.
- Discord IDs are durable deduplication keys. Identical signal payloads posted under different IDs are suppressed during their TTL. Reposting later can create a new trade.
- Jobs are claimed atomically once. A lost claim response leaves the job `claimed`; it is not leased or redelivered. This deliberately favors missed trades over duplicate trades.
- A worker persists intent before broker submission and stores results before acknowledging. A lost acknowledgement is retried without placing another trade.
- Broker timeout, partial fill, non-DONE response, or interrupted submission becomes `uncertain`. That worker blocks new entries until an operator reconciles broker positions and deal/order history. BE monitoring still attempts to protect matching copier positions, including uncertain partial fills.
- No automatic uncertain-order resolution or resend is provided. Do not delete journals to clear a block. Preserve records, inspect MT5 history, and make a reviewed recovery decision. A stopped/crashed worker can leave a `.lock`; remove it only after verifying its process is no longer running.
- In v0.1, completed trade records remain in the local BE monitor scan. Archive/reconciliation tooling and monitoring-scale improvements are production follow-up work.

## Demo acceptance before extending to live accounts

Use two broker demo accounts and a test Discord channel. Confirm buy/sell entries, lot rounding, broker symbol suffix/filling mode, actual-fill BE movement and SL/TP preservation, follower token isolation, paused intake, stale messages, duplicate signals, wide spreads, price gaps, account switching, disconnects, partial fills, and restart around submission/acknowledgement. Compare central logs, local journals, and MT5 deal history. Confirm BE can identify positions using the copier magic/comment on your broker; brokers may alter comments, in which case automatic BE needs a reconciled position mapping.

Production work still needed: broker-tested durable deal-to-position reconciliation, follower consent/onboarding and revocation, secret rotation, deployment/service supervision, alerts, daily loss and equity-drawdown limits, operator recovery UI, load testing, and any requirements applicable to operating the community service. This is an integration MVP, not a deployed or production-approved trading system.

## Reference documentation

- [MetaQuotes Python integration](https://www.mql5.com/en/docs/python_metatrader5)
- [MT5 order submission](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py)
- [MT5 profit/loss estimation](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py)
- [Official Discord gateway documentation](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway.mdx)
- [Exness MT5 settings](https://get.exness.help/hc/en-us/articles/12325499337756-MT5-Settings-and-preferences)
- [PrimeXBT MT5 accounts](https://primexbt.com/platforms/mt5/pro)
