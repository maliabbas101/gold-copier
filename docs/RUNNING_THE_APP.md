# Running the App End to End

This guide takes a new user from a fresh copy of the codebase to a running app. It does not assume you have read any other document. Follow it top to bottom for a first run. Later sections are optional and can be skipped.

In this guide, **project folder** means the folder that contains `pyproject.toml`, `README.md` and the `goldcopier/` package. Every command in this guide is run from that folder.

---

## 1. What you are going to run

| Part | Command | Required? | What it does |
| --- | --- | --- | --- |
| Web server and API | `python -m goldcopier.manage run` | Yes | Serves the web app on `http://127.0.0.1:8001`. Creates the database and the first operator account on first start. |
| Local MT5 worker | started by `manage run` | Only for broker copying | Reads a logged-in MetaTrader 5 terminal and places copied trades on it. Windows only. |
| Discord listener and log sender | started by `manage run` when `DISCORD_BOT_TOKEN` is set | Optional | Reads signal messages from Discord and posts trade logs back. |
| Demo preview | `python -m goldcopier.launch --demo` | Optional | Seeded sample data on `http://127.0.0.1:8000`. No broker execution. |
| Separate worker | `python -m goldcopier.platform_agent` | Optional | Runs an account worker in its own window instead of inside `manage run`. |
| Supervisor | `python -m goldcopier.supervisor` | Optional, operators only | Assigns managed accounts to a pool of terminals. |

The default local run is one terminal window running `manage run`, plus a web browser.

**Safety defaults.** Live trading is off by default (`ALLOW_LIVE_TRADING=false`). Use a broker **demo** account for all testing.

---

## 2. Prerequisites

| Item | Needed for | Notes |
| --- | --- | --- |
| Python 3.11 or 3.12 | Everything | The project requires Python 3.11 or newer. Use 3.12 if you can; CI runs on 3.11. Newer versions may work but are not what the project tests. Check with `python --version`. |
| Git (optional) | Getting the code | Only if you clone the repository. |
| A web browser | Using the app | Any modern browser. |
| MetaTrader 5 terminal (Windows) | Broker copying | Install from your broker or MetaQuotes. Use a demo account. |
| Discord bot | Discord signals or logs | Optional. See section 8. |
| Node.js 22 | Frontend tests only | Optional. Not needed to run the app. |
| Docker Desktop | Container hosting only | Optional. See section 13. |

**Operating system:**

- **Windows 10/11:** everything works, including the MT5 worker. Commands below are shown for PowerShell.
- **macOS or Linux:** the web app, demo, tests and Discord features run. The MT5 worker is Windows only, so `manage run` starts the server without copying. Commands below have Unix equivalents.

---

## 3. Get the code

Either clone the repository or unzip a copy. Then open a terminal in the project folder.

```sh
git clone <repository-url> gold-copier
cd gold-copier
```

Check you are in the right place. These should exist:

```sh
ls pyproject.toml README.md goldcopier
```

Windows PowerShell:

```powershell
Set-Location "<path-to-project-folder>"
Test-Path .\pyproject.toml   # must print True
```

Replace `<path-to-project-folder>` with the real path. Keep the quotes if the path contains spaces.

> Every new terminal window must start in the project folder again. The app reads `.env` and writes `.local-runtime/` relative to the folder you start it from.

---

## 4. Create a virtual environment and install the app

Create the virtual environment once:

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
```

If `py -3.12` is not available, use `python -m venv .venv` with a Python 3.11 or 3.12 interpreter.

macOS or Linux:

```sh
python3.12 -m venv .venv
```

Install the project and its dependencies. Use `[mt5]` only on Windows where you will run the MT5 worker:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
```

macOS or Linux:

```sh
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

Verify the install:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -c "import fastapi, uvicorn, cryptography; print('deps ok')"
```

macOS or Linux:

```sh
.venv/bin/python -c "import fastapi, uvicorn, cryptography; print('deps ok')"
```

Expected output: `deps ok`.

> In this guide, `PY` means the venv interpreter: `.\.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on macOS and Linux. Replace `PY` with the one for your system in every command.

> You do not need to activate the venv. Calling `PY` directly avoids PowerShell execution-policy blocks.

If an install fails with a build error, delete `.venv`, recreate it with Python 3.12, and install again.

---

## 5. Create the settings file `.env`

Copy the example file to `.env`:

Windows PowerShell:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

macOS or Linux:

```sh
[ -f .env ] || cp .env.example .env
```

Open `.env` in any text editor and read section 5.1 before changing anything.

### 5.1 Settings you can leave blank for a first run

A first run works with every setting blank, as long as the MT5 terminal is logged in with **Save password** ticked. The app generates the rest on first start:

- the operator login (`admin@goldcopier.local`) and its password,
- the vault key used to encrypt broker credentials,
- the worker token, database and local URL.

### 5.2 Settings you may set

| Variable | Purpose | Blank means |
| --- | --- | --- |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Your operator login. Password 12 to 128 characters. | `admin@goldcopier.local` with a generated password saved to `.local-runtime/admin-password`. |
| `ADMIN_NAME` | Display name of the operator. | `Local operator`. |
| `MT5_TERMINAL_PATH` | Full path to `terminal64.exe`. Do not quote Windows paths. | The only `terminal64.exe` under Program Files. If none is found, the MT5 worker is off. |
| `MT5_LOGIN`, `MT5_SERVER`, `MT5_PASSWORD` | Broker login. `MT5_PASSWORD` is the **trading** password. Only needed if MT5 has no saved login. | Read from the logged-in terminal. |
| `MT5_SYMBOL` | Gold symbol at your broker, for example `XAUUSDm`. | `XAUUSD` or the detected variant. |
| `ANALYST_NAME` | Display name for your signal feed. | `My signal feed`. |
| `DISCORD_BOT_TOKEN` | Enables the Discord listener and log sender. | Discord is disabled. Copying still works. |
| `DISCORD_CHANNEL_ID`, `DISCORD_TRADER_ID` | Discord signal input. Set both when reading signals from Discord. | Discord signal input is off. |
| `DISCORD_GUILD_ID` | Server ID. | Looked up from the channel. |
| `PUBLIC_URL` | Web address. Local runs must use `http://127.0.0.1:8001`. | `http://127.0.0.1:8001`. |
| `ALLOW_LIVE_TRADING` | Permits live broker accounts. Keep `false`. | `false`. |
| `BILLING_ENABLED` | Enables Stripe billing. Keep `false` locally. | `false`. |
| `LOCAL_STATE_DIR` | Folder for local state. | `.local-runtime`. |
| `PLATFORM_DB` | Path to the SQLite database. | `<LOCAL_STATE_DIR>/platform.db`. |

Rules for `.env`:

- One `KEY=value` per line. Lines starting with `#` are comments.
- Quotes are optional. Windows paths are read literally, so `C:\MT5\terminal64.exe` works as written.
- Shell environment variables override `.env`. If you set `$env:NAME` (PowerShell) or `export NAME=` (shell) earlier, open a new terminal before starting the app.

> Never commit `.env` or `.local-runtime/`. Both are listed in `.gitignore`.

---

## 6. Set up MetaTrader 5 (Windows, broker copying only)

Skip this section if you only want the web app or Discord signals.

1. Install MetaTrader 5 from your broker or MetaQuotes.
2. Open the terminal and log in to a **demo** account: **File → Login to Trade Account**. Tick **Save password**, then click **Login**.
3. Enable algorithmic trading: **Tools → Options → Expert Advisors**, tick **Allow algorithmic trading**, then **OK**. Make sure the **Algo Trading** toolbar button is green.
4. Leave the terminal open and logged in while the app runs.
5. If the terminal is not under Program Files, or you have several installs, set `MT5_TERMINAL_PATH` in `.env` to the full path of its `terminal64.exe`.

Check the path in PowerShell:

```powershell
Test-Path "C:\Program Files\MetaTrader 5\terminal64.exe"   # replace with your path
```

Expected: `True`.

---

## 7. Start the app (the main run)

### 7.1 Start the server

Keep this command running in its own terminal window. Start it from the project folder:

```sh
PY -m goldcopier.manage run
```

Replace `PY` as described in section 4. On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m goldcopier.manage run
```

What happens on the first start:

1. The database and local state folder `.local-runtime/` are created.
2. An operator account is created. The log says where its password is saved (`.local-runtime/admin-password`, or `.env` if you set `ADMIN_PASSWORD`).
3. A signal feed (analyst) and an account record are created. If MT5 is found, its login and gold symbol are read from the terminal.
4. The web server starts. When the log shows `Open http://127.0.0.1:8001/#login`, it is ready.
5. The Discord listener starts only if `DISCORD_BOT_TOKEN` is set. The MT5 worker starts only if a terminal is found on Windows.

Expected warnings when something is off-by-design:

- `DISCORD_BOT_TOKEN is empty; Discord listener is disabled.`: normal when you skip Discord.
- `No MT5 terminal found; local worker is disabled.`: normal on macOS and Linux, or on Windows without MT5. Copying is off until you fix the terminal path.

Stop the server with **Ctrl+C**. This stops the web server, the Discord listener and the local worker.

### 7.2 Get the operator password (first start only)

Open a second terminal in the project folder:

Windows PowerShell:

```powershell
Get-Content .\.local-runtime\admin-password
```

macOS or Linux:

```sh
cat .local-runtime/admin-password
```

If you set `ADMIN_PASSWORD` in `.env`, use that value instead. Do not share or commit it.

### 7.3 Sign in

1. Open `http://127.0.0.1:8001/#login` in a browser.
2. Email: `admin@goldcopier.local`, or the `ADMIN_EMAIL` you set.
3. Password: from 7.2 or your `ADMIN_PASSWORD`.

### 7.4 Start copying (broker accounts only)

1. Open **Accounts**. Your MT5 account is listed and follows your analyst feed.
2. Wait until the account shows **Ready**. Review its risk settings in **Risk & execution**.
3. Click **Start copying**. Copying never starts by itself.
4. Wait until the account shows **Copying**.

Starting copying does not place a trade. It enables future valid signals, subject to risk limits and broker checks. Missed signals are not replayed.

### 7.5 Check the app works

- **Overview**: account state and equity.
- **Trades & orders**: the result of each copied trade.
- **Activity**: plain-language log of actions and problems.

### 7.6 Stop and restart

- **Stop:** Ctrl+C in the server window.
- **Restart:** run the same command from 7.1. Your account, feed, tokens and settings are saved.
- **Before stopping:** pause copying in the browser if you want new entries to stop cleanly. Software-managed breakeven and partial exits need the server running.

---

## 8. Optional: Discord setup

Use Discord for signal input, trade logs, or both. Copying from a broker account does not require Discord.

### 8.1 Create the bot (Discord Developer Portal)

Discord's labels may change over time. These are the names used by this app.

1. Open the Discord Developer Portal, create an application, and name it.
2. Go to **Bot**, click **Reset Token**, confirm, and copy the token. This is `DISCORD_BOT_TOKEN`.
3. On the same **Bot** page, turn on **Message Content Intent** and save.
4. Go to **OAuth2 → URL Generator**. Tick the scope **bot**. Tick the permissions **View Channel**, **Send Messages** and **Read Message History**. Open the generated URL, pick your server and authorise the bot.

### 8.2 Get the IDs

1. In Discord, go to **User Settings → Advanced** and turn on **Developer Mode**.
2. Right-click the signal channel → **Copy Channel ID**. This is `DISCORD_CHANNEL_ID`.
3. Right-click the person or bot who posts the signals → **Copy User ID**. This is `DISCORD_TRADER_ID`.

### 8.3 Put the values in `.env`

```dotenv
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_CHANNEL_ID=your-channel-id
DISCORD_TRADER_ID=the-signal-poster-user-id
```

Restart `manage run`. The log should show that the Discord listener started. The bot needs **View Channel** and **Send Messages** in any channel used for logs.

---

## 9. Optional: sign in as separate users (members and masters)

The operator account from section 7 is an admin. Its sidebar also shows **Operator console** (`#operator`) and **Fleet dashboard** (`#fleet`). Other users see only their own workspace.

To test a second user:

1. Open `http://127.0.0.1:8001/#login` in a private browser window.
2. Sign up with a new email and password.
3. Use the account as normal. Only the admin can open `#operator` and `#fleet`; those pages return 403 for other users.

Community features (a master creating a community, sharing an invite, and subscribers joining) are in the **Community** page (`#community`). Invite links open at `#join/...`.

---

## 10. Optional: separate worker (own window)

Use this when the MT5 worker should run in its own window, or on another Windows machine that talks to the server. Do **not** run it at the same time as `manage run` for the same account.

1. Issue a worker token on the machine that holds the database. The server does not have to be running:

   ```sh
   PY -m goldcopier.manage worker-token
   ```

   If you have several self-hosted accounts, add `--account ACCOUNT_ID`.

2. Copy the printed `AGENT_TOKEN=...` line into `.env`. Also set:

   ```dotenv
   COPIER_URL=http://127.0.0.1:8001
   MT5_TERMINAL_PATH=C:\MT5-Worker\terminal64.exe
   WORKER_STATE=platform-worker.db
   ALLOW_LIVE_TRADING=false
   ```

   Use the same database as the server (`PLATFORM_DB`).

3. Start the worker in its own terminal:

   ```sh
   PY -m goldcopier.platform_agent
   ```

Issuing a token pauses copying. In the browser, click **Start copying** again afterwards.

---

## 11. Optional: supervisor (operators only)

The supervisor assigns managed accounts to a pool of MT5 terminal installations. Run only **one** instance.

Add these to `.env` on the platform and supervisor host:

```dotenv
SUPERVISOR_TOKEN=a-long-random-secret
COPIER_URL=https://your-service.example
TERMINAL_POOL=terminal-pool.json
```

`terminal-pool.json` format is in `deploy/terminal-pool.example.json`. Each terminal must be a separate installation. Start it:

```sh
PY -m goldcopier.supervisor
```

Keep `SUPERVISOR_TOKEN` secret. It can provision every managed account.

---

## 12. Optional: demo preview (no broker)

Seeded sample data with no broker execution. It uses its own database under `.demo/`, so it can run alongside the main app.

```sh
PY -m goldcopier.launch --demo
```

Open `http://127.0.0.1:8000/#demo` for an anonymous view. To edit as the demo user, sign in with the credentials in `.demo/access-password`. Stop with Ctrl+C.

---

## 13. Hosting with Docker (production outline)

This is for deploying a hosted service, not for a local run. Docker and a reverse proxy with TLS are required. Read `docs/PLATFORM.md` for the full production checklist before inviting customers.

1. Copy `.env.example` to `.env`. Set:

   ```dotenv
   PUBLIC_URL=https://your-domain
   DEMO_MODE=false
   ALLOW_LIVE_TRADING=false
   ```

   Configure billing, SMTP and terminal-pool secrets only when you need them.

2. Generate a vault key and copy it into `.env` as `VAULT_KEY=...`. Back it up separately from the database:

   ```sh
   PY -m goldcopier.manage key
   ```

3. Start the web service:

   ```sh
   docker compose up -d web
   ```

   The API binds to host loopback only. Put a TLS reverse proxy in front of it. `deploy/Caddyfile` is an example.

4. Create the operator account. This command prompts for a password:

   ```sh
   docker compose exec web python -m goldcopier.manage create-admin --email operator@example.com --name "Community Operator"
   ```

5. Optional Discord listener in its own container:

   ```sh
   docker compose --profile discord up -d discord
   ```

Stop with `docker compose down`. MT5 terminals run on Windows hosts, not in the container.

---

## 14. Backups and the vault key

- **Database backup** (safe while the app runs):

  ```sh
  PY -m goldcopier.manage backup --output backup-YYYYMMDD.db
  ```

  The output file must not already exist.

- **Vault key:** `.local-runtime/vault-key` (or the value of `VAULT_KEY`) decrypts stored broker credentials. Back it up separately. Without the original key, existing encrypted accounts cannot be opened, and the app refuses to replace the key.

---

## 15. Tests

Install the test tools once:

```sh
PY -m pip install -e ".[dev]"
```

Run the Python tests:

```sh
PY -m pytest -q
```

Expected: a final line ending in `passed`.

Frontend tests need Node.js 22:

```sh
npm ci --ignore-scripts
npm test
node --check goldcopier/web/app.js
```

On Windows PowerShell, use `npm.cmd` instead of `npm` if PowerShell blocks `npm.ps1`.

---

## 16. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `No module named 'fastapi'` or `'cryptography'` | Dependencies not installed. Run the install in section 4. |
| `python` not found or wrong version | Use the Python 3.11 or 3.12 interpreter explicitly (`py -3.12` on Windows). Recreate `.venv`. |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Not needed. Call `.\.venv\Scripts\python.exe` directly. |
| `Port 8001 is already in use` | Another program or a second `manage run` is using the port. On Windows: `netstat -ano \| findstr :8001`. On macOS or Linux: `lsof -i :8001`. Stop that process, or change `PUBLIC_URL` to another loopback port. |
| `Runner lock exists` | A previous run did not exit cleanly. Confirm no server window is still open and no leftover Python process belongs to the app. Then delete `.local-runtime/runtime.lock`. The app removes stale locks automatically when the old process has exited. |
| `Existing encrypted credentials require their original VAULT_KEY` | Your `.local-runtime/vault-key` was deleted or replaced. Restore the original key. |
| `No MT5 terminal found` | Set `MT5_TERMINAL_PATH` in `.env` (Windows). |
| `MT5 worker requires Windows` | Expected on macOS and Linux. Copying needs a Windows machine with MT5. |
| `MetaTrader5` import error | Install with `pip install -e ".[mt5]"` on Windows. |
| `Invalid operator email/password in .env` | Email must be valid. Password must be 12 to 128 characters. |
| `DEMO_MODE` or preview errors on `manage run` | Set `DEMO_MODE=false`. Use a fresh `PLATFORM_DB`, not a seeded preview database. |
| Settings in `.env` ignored | A shell environment variable has the same name. Open a new terminal. |
| Account shows **Connection problem** | Read the server window for the MT5 error. Fix the broker login, password or trading permission, then restart. |
| Discord says disabled | Expected unless `DISCORD_BOT_TOKEN` is set. |
| Page looks stale after an update | Hard-refresh the browser (Ctrl+F5 on Windows, Cmd+Shift+R on macOS). |

---

## 17. Files you must never commit

Keep these out of version control. The project's `.gitignore` already lists them. Do not remove those entries.

- `.env`
- `.local-runtime/` (contains the database, vault key and admin password)
- `.demo/`
- `.venv/`
- `*.db`, `*.db-*` (database files and backups)
- `config.json`, `agent.json` (local configuration)

---

## 18. Quick reference

Start the server:

```sh
PY -m goldcopier.manage run
```

Open the app: `http://127.0.0.1:8001/#login`

Read the first-start password:

```sh
cat .local-runtime/admin-password
```

Create an operator account on an existing database:

```sh
PY -m goldcopier.manage create-admin --email you@example.com --name "Your Name"
```

Demo preview: `PY -m goldcopier.launch --demo`

Tests: `PY -m pytest -q` and `npm test`

Stop: Ctrl+C in the server window.
