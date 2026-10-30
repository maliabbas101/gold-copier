# Gold Copier

Copy an authorized analyst's Discord gold signals to your MT5 account.
**Fill three Discord lines in `.env`, start the server, and click Start copying.**
The server finds MT5, registers your account and runs the Discord listener and
MT5 worker for you.

## 1. Install once

Use Windows with Python 3.11 or 3.12 and an installed MT5 terminal. In MT5, log in
to your broker's **demo account** (hedging mode) with **Save password** ticked, and
enable **Algo Trading**. Open PowerShell in this project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

## 2. Fill in `.env`

Only these three lines are required:

```dotenv
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_CHANNEL_ID=your-signal-channel-id
DISCORD_TRADER_ID=the-signal-authors-user-id
```

The bot must be in your server, able to read the signal channel, and have
**Message Content Intent** enabled. `DISCORD_TRADER_ID` is the human or bot that
posts the signals. With Discord Developer Mode on, right-click the channel or user
and choose **Copy ID**.

Everything else is detected or generated on first start:

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
signals from your selected analyst, subject to your risk settings and broker
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
