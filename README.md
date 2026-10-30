# Gold Copier

Copy an authorized analyst's Discord gold signals to your MT5 account.
**Fill `.env`, start the server, connect MT5 in the app, and click Start copying.**
The server manages the Discord listener and MT5 worker for you.

## 1. Install once

Use Windows with Python 3.11 or 3.12 and an installed MT5 terminal. Have your
broker's **MT5 demo account** (hedging mode) number, server and trading password ready.
Open PowerShell in this project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

## 2. Fill in `.env`

Edit **`.env`**, not `.env.example`. The template labels each setting as **required**,
**conditionally required**, **default**, or **optional**.

| Variables | Do I need to fill them in? |
| --- | --- |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | **First start only.** Creates your app login; password must be 12-128 characters. |
| `DISCORD_BOT_TOKEN` | **Required for Discord signals.** Blank disables the listener. |
| `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `DISCORD_TRADER_ID` | **Required for a new analyst.** Set all three together. Optional if that source already exists in the app. |
| `MT5_TERMINAL_PATH` | **Required for local MT5.** Use the actual executable path; blank disables the worker. |
| `MT5_PASSWORD` | **Required unless MT5 already has a working saved login.** Use the trading password. |
| `ANALYST_NAME` | **Optional.** You can keep the supplied display name. |

Example:

```dotenv
ADMIN_EMAIL=you@example.com
ADMIN_PASSWORD=choose-a-password-at-least-12-characters

DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_GUILD_ID=your-discord-server-id
DISCORD_CHANNEL_ID=your-signal-channel-id
DISCORD_TRADER_ID=the-signal-authors-user-id
ANALYST_NAME=My analyst

MT5_TERMINAL_PATH="C:\Program Files\MetaTrader 5\terminal64.exe"
MT5_PASSWORD=your-mt5-trading-password
```

The Discord bot must be in your server, able to read the signal channel, and have
**Message Content Intent** enabled. `DISCORD_TRADER_ID` identifies the human or
bot posting the signals. The initial analyst is created from these IDs automatically.

`MT5_PASSWORD` is your **trading account password**, not your broker website
password or an investor password. You can leave it blank if that terminal already
has a working saved login. MT5 must allow algorithmic and external Python trading.

**Leave the other defaults unchanged** for your first setup: `PUBLIC_URL`,
`PLATFORM_DB`, `LOCAL_STATE_DIR`, `DEMO_MODE`, `ALLOW_LIVE_TRADING` and
`BILLING_ENABLED`. `MT5_FILLING_MODE=1` uses IOC; change it only if your broker
requires another supported filling mode.

**Leave these optional values blank** for a new local setup: `VAULT_KEY`,
`AGENT_TOKEN`, `COPIER_URL`, `WORKER_STATE` and `LOCAL_ACCOUNT_ID`. The runner
handles the key, token, URL and journal automatically. Account selection only
needs an explicit ID if multiple local accounts exist and no token selects one.
Preserve the original vault key when using an existing encrypted database.

The Stripe, SMTP, managed-pool and master-bridge settings at the bottom of
`.env.example` are **optional features**, not requirements for local Discord copying.
Existing installations can still supply their token and journal path; see the
[advanced setup guide](docs/PLATFORM.md#one-command-local-windows-startup).

## 3. Start the server

```powershell
.\.venv\Scripts\python.exe -m goldcopier.manage run
```

Leave this window open. There are no separate worker commands or setup prompts.
Open **[http://127.0.0.1:8001](http://127.0.0.1:8001/#login)** and sign in with the
`ADMIN_EMAIL` and `ADMIN_PASSWORD` from `.env`. These create your initial app
account only; changing them later does not reset its password.

## 4. Connect MT5 and start copying

1. Open **Accounts** and click **Connect MT5**.
2. Enter your MT5 account number, exact broker server and gold symbol (for example,
   `XAUUSD` or `XAUUSDm`). Choose **Demo account** and **This computer (automatic)**.
3. Select your analyst under **Copy signals from**, confirm account ownership, and
   click **Connect MT5**.
4. The account page updates automatically. Wait for **Ready**, review the risk
   settings, then click **Start copying**.
5. When the account shows **Copying**, wait for a new valid signal. Check
   **Trades & orders** for its result and MT5 for the actual position.

Starting copying does not immediately place a trade. It enables future valid
signals from your selected analyst, subject to your risk settings and broker
checks. Old Discord messages are not replayed. Supported signal examples are in
[the signal guide](docs/PLATFORM.md).

## Next time

Run the same server command. Your account, analyst, tokens and settings are saved.
Keep `.env` and `.local-runtime/` private and preserve them between starts.
**Pause copying** stops new entries. **Ctrl+C** stops the server and its services;
software-managed breakeven and partial exits need the server running.

If the account shows **Connection problem**, check the server window for the MT5
error, correct the account/server/password or trading permissions, and restart.
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
