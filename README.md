# Gold Copier

Trade once on a trader's MT5 or eligible Exness API account. The copier sends protected new fills directly to followers and posts Discord logs separately. **You do not enter each trade in Discord and then in the operator console.**

## Recommended setup: broker → copier → followers

1. **Install** using the Windows instructions below for MT5. Start `python -m goldcopier.manage run`, then sign in to the local frontend. The direct Exness API worker can run without Windows/MT5. Source and follower accounts must be separate.
2. In **Accounts**, connect the trader's account and choose **Trader source — read-only observation**. An operator may also convert their own paused follower account through source setup if it has no copier-owned exposure or unresolved work. Use the master terminal's saved login or a read-only/investor password where possible. Eligible direct Exness source keys should have read-only scopes.
3. In **Operator console → Add analyst feed**, choose **Direct broker copying**, give it a name, and save. Existing feeds can also be reused. Then click **Configure trader source**, select the trader account and this analyst, and confirm. Enter a Discord **log channel ID** if you want outbound logs. Discord author/guild IDs are unnecessary for direct execution.
4. Connect each **follower** account, follow that analyst, configure its broker symbols and risk limits, wait for Ready, then click **Start copying**. The trader source is read-only and cannot follow itself. Map symbols on the source account too; its mappings translate actual broker symbols into the community's canonical pairs.
5. Keep the trader watcher and follower workers running. **Place a new trade with an SL on the trader terminal.** The watcher detects the fill, the server queues follower market entries, and workers execute within each follower's limits. No signal-console form is involved. The initial source snapshot is baselined: existing positions are not copied just because you connected or restarted.

If both MT5 accounts are on one Windows computer, use two separate terminal installations and saved broker logins:

```dotenv
MT5_TERMINAL_PATH=C:\MT5-Follower\terminal64.exe
MASTER_TERMINAL_PATH=C:\MT5-Trader\terminal64.exe
# Optional if there is more than one account per role:
LOCAL_ACCOUNT_ID=your-follower-account-id
MASTER_ACCOUNT_ID=your-trader-account-id
# Optional if the trader terminal does not have a saved login:
MASTER_PASSWORD=your-trader-read-only-password
```

Restart `manage run` after changing terminal paths. It starts a separate worker per role. With one local trader account and remote follower workers, the single local terminal can observe the trader. For separately hosted workers, use **Accounts → Worker setup** and run `python -m goldcopier.platform_agent` with that account's own `AGENT_TOKEN`, `MT5_TERMINAL_PATH` (MT5 only), and a separate `WORKER_STATE` file. The account's server-side purpose determines read-only source versus follower behavior. A token cannot choose another analyst or account.

### What is copied, and what is not

New **filled** positions are copied; unfilled master pending orders are not cloned. When the master pending order fills, its new position is copied at follower CMP. Source detection polls about every 250 ms on MT5 and follows available Exness snapshots/events; follower queue polling is about one second. Network/broker calls add latency. This is not a zero-latency or tick-level guarantee; a position opened and fully closed between observations may not be mirrored.

A source trade needs a mapped symbol and an SL. If the trader adds the SL shortly after opening, the watcher waits up to 60 seconds; it never invents protection or copies an expired trade. Later SL/TP changes, actual-fill breakeven, proportional partial reductions, and broker-confirmed full closes are propagated with stable references. Source volume increases/reversals in an existing position are blocked: use separate hedging positions for new entries. Removing source SL/TP does not remove follower protection. Missing positions alone never trigger a close; MT5 close history or Exness closed-position evidence is required. If confirmation is unavailable, the source status says it is waiting and follower SL/TP remain in place. A source-side netting/aggregation pattern is not equivalent to separate hedged trades.

Each follower keeps its own sizing and exposure limits; the master lot amount is not copied blindly. Missed entries are not replayed when a follower later connects or enables copying. Pause/disable stops future source events; existing follower positions still need management. Read **Accounts → trader source status**, **Activity**, and **Trades & orders → Execution history** for precise waiting/skipped/rejected outcomes. A source event marked delivered is distinct from a broker-confirmed follower execution.

### Discord is a log, not a dependency

```dotenv
DISCORD_BOT_TOKEN=your-existing-bot-token
```

Give the bot **View Channel / Send Messages** permission in the selected log channel. `manage run` starts the independent log sender. On separately deployed servers run `python -m goldcopier.discord_logs` beside the API using the same `PLATFORM_DB`. Source events and follower outcomes enter a durable outbox; rate limits, network failures, and bot restarts retry separately. Copying continues when Discord is unavailable. No `@everyone` ping or follower login/credential is published. Logs start with `[Gold Copier log]` and cannot be consumed as a new signal, preventing a copy loop. Long outages can produce repeated informational logs; they never produce duplicate trades.

No Discord token or channel is required to **execute** the direct path. The operator console shows log configuration/backlog errors. Configure a log channel to release logs previously held without a destination.

### If you only have Discord signals

Discord-message copying remains an alternative: set the feed to **Read Discord messages**, configure exact guild/channel/author IDs, and let the listener ingest new posts automatically. **Do not paste the post into the operator console again.** The console's **Manual test (optional)** form is only for a new test, with an automatically generated unique reference. Direct broker feeds reject manual/Discord input.

A duplicate notice means the same source ID was already consumed; it does not mean the trade executed. Find that source in Activity and Execution history instead of entering it twice. Follow the shared installation steps below, then choose the direct source setup above or the optional Discord-message configuration.

## 1. Install once

Use Windows with Python 3.11 or 3.12 and an installed MT5 terminal. In MT5, log in
to your broker's **demo account** (hedging mode) with **Save password** ticked, and
enable **Algo Trading**. Open PowerShell in this project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

## 2. Optional Discord-message configuration

For the **Discord-message path only**, configure these three lines. The recommended direct path does not need them for execution:

```dotenv
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_CHANNEL_ID=your-signal-channel-id
DISCORD_TRADER_ID=the-signal-authors-user-id
```

The bot must be in your server, able to read the signal channel, and have
**Message Content Intent** enabled. `DISCORD_TRADER_ID` is the human or bot that
posts the signals. With Discord Developer Mode on, right-click the channel or user
and choose **Copy ID**.

Shared local settings are detected or generated on first start:

| Setting | Automatic value | Fill it in when |
| --- | --- | --- |
| `MT5_LOGIN`, `MT5_SERVER`, `MT5_PASSWORD` | Read from the logged-in MT5 terminal | MT5 does not have a saved login. Use the **trading** password. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | `admin@goldcopier.local`, password saved in `.local-runtime/admin-password` | You want your own login (password 12-128 characters). |
| `MT5_TERMINAL_PATH` | The only `terminal64.exe` under Program Files | You have several MT5 terminals installed. |
| `MT5_SYMBOL` | `XAUUSD` or your broker's variant, e.g. `XAUUSDm` | Your broker's gold symbol is not detected. |
| `DISCORD_GUILD_ID` | Looked up from the channel | Never, normally. |
| `ANALYST_NAME` | `My signal feed` | You want another display name. |

Leave the rest blank. The vault key, worker token, database and URL are handled
automatically. Stripe, SMTP, managed-pool and master-bridge features are optional;
see the [advanced setup guide](docs/PLATFORM.md#one-command-local-windows-startup).

## 3. Start the server

```powershell
.\.venv\Scripts\python.exe -m goldcopier.manage run
```

Leave this window open. On first start it creates your login, the analyst and
your MT5 account, and connects the worker. The window tells you where your
password is saved. Open **[http://127.0.0.1:8001](http://127.0.0.1:8001/#login)** and sign in.

## 4. Start copying

1. Open **Accounts**. Your MT5 account is already listed and follows your analyst.
2. Wait for **Ready**, review the risk settings, then click **Start copying**.
3. When the account shows **Copying**, wait for a new valid signal. Check
   **Trades & orders** for its result and MT5 for the actual position.

Starting copying does not immediately place a trade. It enables future valid
events from your selected direct broker source or Discord analyst, subject to your risk settings and broker
checks. Old Discord messages are not replayed. Supported signal examples are in
[the signal guide](docs/PLATFORM.md).

## Next time

Run the same server command. Your account, analyst, tokens and settings are saved.
Keep `.env` and `.local-runtime/` private and preserve them between starts.
**Pause copying** stops new entries. **Ctrl+C** stops the server and its services;
software-managed breakeven and partial exits need the server running.

If the account shows **Connection problem**, check the server window for the MT5
error, fix the MT5 login, password or trading permissions, and restart.
Use a fresh PowerShell window if you previously set `$env:` values, because those
override `.env`. One local terminal supports one account at a time.

## Development and deployment

- [Production, multiple terminals, troubleshooting and signal format](docs/PLATFORM.md)
- [Feature coverage](docs/FEATURES.md)
- [Read-only UI preview](docs/frontend-preview.html) (cannot execute trades)
- Frontend source: [goldcopier/web/](goldcopier/web/); no Node build is needed to run it.

Developer checks:

```powershell
python -m pytest -q
npm.cmd ci --ignore-scripts
npm.cmd test
```

Automated execution tests use a simulated MT5 API. Verify the complete Discord-to-MT5
flow on a broker demo account before considering live use. Live trading is disabled
by default.

## Multi-instrument update and Exness API

Open **Trading pairs** for canonical pair/alias CRUD and per-account broker mappings, with per-instrument spread, entry-distance, and lot limits. Multiple distinct signals, including identical-price signals with different message IDs, are queued independently. A single message may contain up to 20 complete signal blocks separated by `---` or starting a new BUY/SELL header on the next line.

**Accounts → Exness API** provisions the official signed direct connector for eligible Exness trading accounts. It is separate from MT5 and remains read-only with live execution disabled. See [docs/INSTRUMENTS_AND_EXNESS.md](docs/INSTRUMENTS_AND_EXNESS.md) for eligibility, configuration, confirmation semantics, acceptance limits, and migration instructions. No live API credentials were used to test this integration.

## Trader signal formats, CMP, and later updates

The alerts below use your trader's historical prices as **syntax examples**, not current trade recommendations. Send each code block as a separate Discord message from the configured author. Do not paste a whole chat transcript, usernames, timestamps, or commentary into a signal. The parser accepts `@everyone`, `@Forex Alert`, bold markers, bullet targets, and signed pip annotations; it sizes from actual prices and broker contracts, not the printed pip counts. Text in Discord embeds (title, description, and fields) is also read. Images/OCR are unsupported.

### Execute now at CMP

```text
BUY XAUUSD CMP
SL : 3894.7 (-60 pips)
```

CMP means a market request on receipt, using the follower's current **Ask for BUY / Bid for SELL**. **No number is required.** `BUY XAUUSD CMP`, `SELL XAUUSD CMP`, `XAUUSD BUY CMP`, or `XAUUSD BUY` with `ENTRY: CMP` use the follower's fresh broker quote. If you optionally include a number, it remains a reference price and entry tolerance applies. With no number there is no numeric entry-distance filter; stop/target validity, spread, sizing and exposure checks still apply. `XAUUSD BUY CMP` or `XAUUSD BUY MARKET` with `ENTRY: 3900.7` is also accepted.

**SL is mandatory. TP and BE may arrive later.** With no TP, the position initially has only its broker-side SL; no target or automatic BE price is invented. Without a BE trigger the bot does not automatically move the stop, but a later explicit BE command is supported. For an optional manual test only, open **Operator console → Manual test**, choose the **CMP — current market price** template, set the SL/targets and explicitly submit. Selecting a template does not trade. The direct source and Discord listener do not require this step.

### Place a pending order

```text
PLACE XAUUSD SELL LIMIT
Entry : 3909.0
SL : 3920.0 (-110 pips)
TP : 3870.5 (+385 pips)
```

`XAUUSD SELL LIMIT` is equivalent. Use LIMIT for a pullback (BUY below Ask / SELL above Bid), STOP for a breakout (BUY above Ask / SELL below Bid). Pending orders are submitted to the broker immediately and then wait there for a trigger; they are not market orders waiting in the application's queue.

| Entry instruction | Behavior |
| --- | --- |
| `BUY XAUUSD CMP 3900.7` | Market now, subject to tolerance and all risk checks |
| `XAUUSD BUY MARKET` + `ENTRY:` | Same immediate market behavior |
| `XAUUSD BUY` or `XAUUSD BUY AUTO` + `ENTRY:` | Market if within entry tolerance; otherwise choose the corresponding broker LIMIT or STOP |
| `XAUUSD BUY LIMIT` / `STOP` + exact `ENTRY:` | Explicit pending order; rejected if on the wrong side of the current quote |
| AUTO with `ENTRY: 3900 - 3902` | Market within the zone/tolerance; otherwise pending at the nearest zone edge |
| LIMIT/STOP with `ENTRY: 3947 | 3943.5` | Two separate broker orders, with the message's total risk split equally |

This changes the old implicit-market behavior for entries without an order type. Explicit `MARKET`/CMP remains immediate. Upgrade the listener, service, and workers together; existing jobs and historical messages are not reinterpreted or replayed.

### Half risk and two entries

```text
SELL XAUUSD CMP 3940.4
Half Risk (0.5r)
SL : 3951.0 (-106 pips)
```

```text
PLACE XAUUSD BUY LIMIT
Entry : 3947.0 | 3943.5
Half Risk (0.5r)
SL : 3934.5 (-125 pips)
```

`Half Risk (0.5r)` halves the configured risk budget and fixed lot amount; it does not mean 0.5 lots. For a configured 0.5% risk cap, half risk is 0.25% total; the two-entry message receives 0.125% per leg. Minimum lots, rounding, aggregate risk, and position limits still apply. Legs execute independently: one can be accepted while another is rejected. A price separated with `-` is an entry zone; `|` explicitly requests separate legs. Never turn a two-entry alert into a single midpoint order.

### Set TP later and move SL to BE

Reply to the original signal message in Discord, or add `SIGNAL: <original message ID>` as below. Replace `123456789` with the real original ID; do not copy the example ID into production.

```text
XAUUSD Set TP:
• TP1: 3955.0 (+80 pips)
• TP2: 3975.0 (+280 pips)
SIGNAL: 123456789
```

```text
XAUUSD Move SL to Breakeven (3947.0)
SIGNAL: 123456789
```

The original two-entry message ID updates **both legs**. Append `-1` or `-2` to target one leg. BE always uses each follower's actual fill; the trader's printed `3947.0` is informational. TP1/TP2 use your configured partial-close allocations, and changes propagate to broker pending orders as well as filled copier positions. Software targets and BE need a running worker; broker-side SL remains in place.

An unreferenced symbol update is accepted only when exactly one unclosed signal group exists for that analyst/pair. With overlapping BUY/SELL alerts, missing context, or older unclosed signals, it is rejected visibly; **the bot never guesses that the newest trade is the intended one**. Your sample has overlapping sell and buy instructions, so replies or explicit IDs are necessary. `Stop Loss Hit` / `Trade Closed` status messages close this routing context, but never liquidate a follower's broker account. If the master and follower outcomes differ, use an explicit referenced command.

The existing management syntax still works:

```text
UPDATE SIGNAL: 123456789
SL: 3940
TP1: 3955
TP2: 3975
```

`BE SIGNAL: 123456789`, `CLOSE SIGNAL: 123456789`, `CANCEL SIGNAL: 123456789`, and `PARTIAL SIGNAL: 123456789` plus `PERCENT: 50` are explicit broker instructions. Updates/replies are scoped to the configured analyst and original pair, never another trader's signal.

### Status alerts are not new trades

These are recognized as informational announcements:

```text
XAUUSD Entry Triggered
```

```text
XAUUSD Entry 1 Triggered
```

```text
XAUUSD TP 1 Hit (+80 pips)
```

```text
XAUUSD Stop Loss Hit -110 pips
```

```text
XAUUSD Trade Closed (3947.9)
Result: -75 pips
```

Use the same reply/reference rule if several signals are active. A status message never reopens, doubles a partial close, or substitutes for the follower's broker confirmation. Narrative commentary such as “market is waiting” is not an executable signal. Configure the **APP/bot author's actual ID** if that author posts alerts; the human analyst's ID alone will not authorize another bot. A webhook must itself be explicitly configured by its exact author/webhook ID, guild, and channel; display names and roles do not authorize trades.

## If price was hit but no trade appeared

The previous implicit market entry was checked once at receipt and could reject outside tolerance; it did not wait for price to revisit the entry. Use explicit LIMIT/STOP or the new AUTO behavior when you intend to wait.

1. Open **Activity** for `signal skipped` / `Discord source ... ignored` reasons. Check the exact author/channel, pair mapping, followed analyst, Copying switch, healthy worker heartbeat, and live/demo gate. The listener does not replay old posts or backfill a trigger missed while it was off.
2. Open **Trades & orders → Execution history**. `queued` is waiting for a worker, `rejected` contains the reason, and `uncertain` requires broker reconciliation before any retry. Broker preflight errors now include the broker code/comment. A successfully placed pending order says **waiting for price trigger**, with its broker ticket; it does not mean a position filled.
3. Check **Pending broker orders** and MT5's Trade/History tabs. BUY triggers use Ask and SELL triggers use Bid; touching a chart's displayed price alone may not trigger the order because of spread. Check trading sessions, margin, stop distances, supported lot sizes, and broker permissions.
4. Pair spread/entry limits, daily loss/drawdown protection, max open positions, and aggregate risk can block a valid signal. A “Daily equity loss stop reached” message is a risk rejection, not a parsing error. Confirm the cause on a demo account before changing risk settings.
5. Intake instructions expire after 60 seconds. An accepted MT5 pending order normally expires after one hour; symbols supporting only GTC retain it until explicit cancellation. The direct Exness API pending schema has no expiration field. Inspect broker history to distinguish rejection, cancellation, expiry, and fills.

Without your execution-history result or broker ticket, the exact cause of a particular missed trade cannot be established from the signal text alone. Never resubmit an uncertain operation just because the position is not visible yet.

Official execution references: [MT5 order submission](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py) and [order types / fill policies](https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties).

## Live portfolio and trade updates

**Overview → Live** shows the selected account's rolling 30-minute equity curve. Balance, equity, floating P&L, current position prices, pending orders, and execution outcomes update automatically in **Trades & orders**. The account selector and trade search remain usable while updates arrive; an open management dialog keeps your entered values.

- Keep the service and that account's broker worker running. The worker publishes a fresh account/position snapshot each cycle, normally about once per second; the authenticated browser feed delivers snapshots every second. Broker calls and order confirmation can delay a cycle. This is a sampled portfolio view, not a tick-by-tick exchange feed.
- Trade history and realized P&L refresh within roughly five seconds. MT5 current prices/floating P&L come from the broker. Direct Exness API positions show fresh available quote prices; unavailable per-position P&L remains **— / Not reported** rather than an invented value.
- Pausing new entries does not stop account snapshots. The **Live / Stale / Disconnected** indicator distinguishes current broker updates from old data; a stream connection alone never makes stale broker values live. After ten seconds without a broker snapshot the badge says Stale. Broker prices can stop moving when a market is closed.
- History is collected while the worker runs, independently of browser login. New history samples are saved every five seconds, with older data compacted to minute samples and active-account retention limited to 30 days. **1D / 7D / 30D** request account-specific, time-bucketed history. Periods before the worker was running cannot be reconstructed from login-time data. Existing history is preserved subject to this retention window.
- Streams reconnect automatically, with one-second HTTP polling as a fallback. Hidden tabs release their stream and reconnect when reopened. Logging out or revoking a session ends its feed. Seeded public-demo data remains explicitly labelled sample data.

After upgrading, restart both the service and broker workers, then hard-refresh the browser. With a broker demo account connected, leave **Overview → Live** or **Trades & orders** open and confirm that quote/P&L changes appear without pressing Refresh. No additional frontend build or API key is required.

For hosted installations, allow long-lived `GET /api/portfolio/stream` responses through the reverse proxy and disable response buffering for this route (`X-Accel-Buffering: no` is supplied). The feed uses the existing same-origin HttpOnly session cookie and checks session validity continuously. `GET /api/portfolio/equity?account_id=...&period=LIVE|1D|7D|30D` enforces the same account ownership rules as the dashboard.
