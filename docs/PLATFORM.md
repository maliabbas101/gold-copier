# Platform setup and operations

## Components and trust boundaries

- `goldcopier.platform`: FastAPI application and bundled frontend. Browser session cookies never carry broker passwords.
- `goldcopier.platform_store`: transactional SQLite state and audit events. Use a separate `PLATFORM_DB`, not the legacy v0.1 database.
- `goldcopier.discord_platform`: reads allowlisted human or bot Discord authors/channels, sharing the web database on the same host.
- `goldcopier.platform_agent`: one Windows worker and one dedicated MT5 terminal per follower. Journals, automatic-exit state, and risk baselines persist locally.
- `goldcopier.supervisor`: an optional operator-managed Windows pool of preinstalled terminal slots. A follower can select managed hosting when the operator configures it.
- `goldcopier.master_bridge`: optional read-only MT5 analyst source, independent of the follower terminal pool.

A member owns an account record; their browser can only read or change that account. An account worker token can only access that account's configuration, commands, and execution results. A supervisor token is privileged and can provision **all managed accounts**; do not distribute it to members. A signal-ingest token can submit analyst instructions; protect it like an execution credential.

MT5 passwords are encrypted **at rest** with AES-256-GCM. This is not client-side or end-to-end encryption: the trusted service and authorized worker can decrypt the password. Transport must use HTTPS. Workers need account trading permissions and can place or close positions. They have no withdrawal workflow in this application.

## Local demo

`python -m goldcopier.launch --demo` creates `.demo/platform.db`, `.demo/vault-key`, and `.demo/access-password` with restrictive local permissions and runs on localhost. View `/#demo` anonymously, or sign in using the generated demo account credentials from the local file to test editing and the operator console. No real worker claims are permitted in `DEMO_MODE=true`.

For standalone preview generation from a running seeded demo:

```bash
python scripts/build_preview.py
```

The result is `docs/frontend-preview.html`. It bundles the actual frontend with a read-only sample-data adapter and no credentials. It is a visual review artifact; server-backed editing requires running the actual app.

## One-command local Windows startup

Use `python -m goldcopier.manage run` for a local server, the Discord listener,
and one self-hosted MT5 worker together. Copy `.env.example` to `.env` once and
set `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `MT5_TERMINAL_PATH` and `DISCORD_BOT_TOKEN`.
Install `python -m pip install -e ".[mt5]"` first. The initial operator is created
from `.env` without terminal prompts; existing accounts are never reset or promoted.
Set `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID` and `DISCORD_TRADER_ID` to create the
initial analyst automatically. `ANALYST_NAME` is its display name. Existing matching
sources are reused without changing their paused/enabled state.
The default example URL is **http://127.0.0.1:8001/#login**. The runner is limited
to loopback HTTP; production hosting uses the deployment instructions below.

Add the account in the app as **Self-hosted**, with the login and server matching
the broker demo account you want to connect. `MT5_PASSWORD` can supply its trading
password for automatic login; leave it blank to use the terminal's saved login. The runner discovers
the account and provisions its token automatically. Choose the analyst in the
connection form, wait for Ready, then click Start copying on the account card. With multiple self-hosted
accounts, set `LOCAL_ACCOUNT_ID` to the desired ID shown on the account card before
restarting. Each terminal can serve only one account. Additional simultaneous
accounts need the existing managed terminal pool.

To reuse a worker token you already generated, put it in `AGENT_TOKEN` in `.env`.
The runner uses that token to select its self-hosted account and does not rotate
it. `PLATFORM_DB` must be the database that issued the token. An invalid token or
conflicting `LOCAL_ACCOUNT_ID` is rejected. Leave `AGENT_TOKEN` blank to keep
automatic provisioning. Set `WORKER_STATE` to the existing journal when replacing
an older worker command; blank uses the per-account journal. `COPIER_URL` can be
blank to use `PUBLIC_URL`; if supplied for `manage run`, the two URLs must match.

The runner loads literal `.env` values, preserving quoted Windows paths. Shell
variables override file settings. Blank Discord/terminal settings disable those
components. A custom file can be selected with `--env-file path`. Other management
commands also load this file. Never paste secrets into command output or commit
`.env` or `.local-runtime/`.

When `VAULT_KEY` is blank, a new workspace gets a persistent key at
`.local-runtime/vault-key`. Existing encrypted accounts require their original
key; the runner refuses to replace an unavailable or incorrect key. Local worker
tokens and submission journals are saved per account under `.local-runtime/workers/`.
Restrict access to this directory to the operating user, preserve it between
starts, and back up the key separately from the database. A valid saved token is
reused on restart; issuing a replacement token pauses copying as usual.

**Ctrl+C** stops the local worker, listener and web server. A failed worker pauses
new entries and shows a connection error in the app, which stays available. Fix the
error and restart the server command; workers are not automatically restarted.
A failed web server or Discord listener stops the stack with an actionable error.
After forced shutdown, verify old processes and broker state before removing any
stale lock. Recently active workers are not taken over. A seeded preview database
is rejected, even if `DEMO_MODE` is changed: use a separate workspace database.
`DEMO_MODE=false` permits real terminal integration with an **MT5 demo account**;
`ALLOW_LIVE_TRADING=false` continues to block live accounts.

## Production web service

1. Copy `.env.example` to `.env`. Generate `VAULT_KEY` with `python -m goldcopier.manage key`. Keep it in your secret manager and back it up separately from the database.
2. Set `PUBLIC_URL=https://your-domain`, `DEMO_MODE=false`, and `ALLOW_LIVE_TRADING=false` during broker acceptance. Configure billing, SMTP, and managed-pool secrets only when needed.
3. Build/start `docker compose up -d web`. The API port binds only to host loopback. Install Caddy on the host, update `deploy/Caddyfile`, configure DNS, and issue TLS. Caddy applies a 64 KB request-body cap. Add infrastructure-level abuse controls appropriate to your deployment.
4. Create your operator account interactively:

```bash
docker compose exec web python -m goldcopier.manage create-admin --email operator@example.com --name 'Community Operator'
```

5. Sign in through the frontend. Add authorized Discord analysts in **Operator console**. Start the listener with `docker compose --profile discord up -d discord` after setting its bot token and Message Content Intent.
6. Members create their accounts and follow feeds. Self-hosted workers use account-specific tokens generated through **Accounts → Worker setup**. Managed accounts are provisioned by the Windows pool.
7. Configure logs, service supervision, access controls, encrypted backups, database retention, resource limits, and operator alerting before inviting customers. Activity alerts exist in the UI; automatic SMS/pager/email incident delivery is not configured by this project.

The Docker image contains only the web/listener components; MT5 terminals run on Windows hosts. The image uses a non-root user and a persistent named volume. Use one web service host with local SQLite WAL storage. Never share SQLite files across a network filesystem or run the same account on two workers.

## Self-hosted Windows worker

Install Python 3.11/3.12, the project, and `pip install '.[mt5]'`. Install a dedicated broker MT5 terminal. Use a hedging account and enable algorithmic/external Python trading. Save the worker settings in `.env` in the project directory:

```dotenv
COPIER_URL=http://127.0.0.1:8000
AGENT_TOKEN=replace-with-your-account-worker-token
MT5_TERMINAL_PATH="C:\GoldCopier\Terminal-01\terminal64.exe"
MT5_FILLING_MODE=1
WORKER_STATE=platform-worker.db
ALLOW_LIVE_TRADING=false
```

Set `COPIER_URL` to the actual running API address (HTTPS outside localhost).
The standalone worker reads `.env` automatically, so no PowerShell `$env:...`
assignments are needed. Start it with:

```powershell
python -m goldcopier.platform_agent
```

Use `python -m goldcopier.manage run` instead when you want the server, Discord
listener and local worker started together. Do not run both commands for the same
account. Existing shell variables override `.env`; use a fresh shell when replacing
old PowerShell assignments. The standalone worker requires `AGENT_TOKEN`;
automatic token provisioning is provided only by `manage run`.

For self hosting, set `MT5_PASSWORD` to log in automatically with the account login
and server registered in the app, or leave it blank and use a saved terminal login. A managed worker receives the decrypted trading password over its authenticated configuration endpoint and initializes the terminal with it. The worker verifies login, server, account mode, hedging mode, trading permissions, and fresh quotes. It does not accept whatever account happens to be logged into a terminal.

Filling mode must match your broker: 0 FOK, 1 IOC, 2 RETURN. Pending entries use RETURN and broker-expiring orders with a default one-hour TTL. Broker support for time-specified pending orders must be checked; unsupported instructions fail preflight. Market signal TTL defaults to 60 seconds. A missed entry is not automatically chased or requeued.

After a fresh heartbeat, follow an analyst and click **Start copying**. The service refuses to start an offline/unverified account, an unresolved account, or a live account when the operator gate is disabled. In production with billing enabled, it also checks an active subscription.

## Managed Windows terminal pool

Install the project and a separate MT5 terminal installation for each simultaneous follower slot. Copy `deploy/terminal-pool.example.json` and set real executable paths. Use a restricted Windows service identity and NTFS ACLs for terminal profiles, worker journals, and pool state.

Set `COPIER_URL`, the matching `SUPERVISOR_TOKEN`, `TERMINAL_POOL`, and optional `POOL_STATE`. Run:

```powershell
python -m goldcopier.supervisor
```

The supervisor polls managed-account assignments, allocates unused terminal slots, rotates an account-specific worker token, and starts its dedicated child worker. The member sees verified status in the dashboard and must explicitly start copying. Capacity is limited by your installed terminal slots; there is no automatic purchase/provisioning of cloud servers.

Use **one supervisor instance**. There is no distributed fencing/automatic hot-standby guarantee. On a failed worker, the supervisor retains the slot instead of restarting it repeatedly. Review its broker history and journal before restarting. After process crashes, stale lock files require manual verification that the old process is gone. Pool bindings are persistent and reserved until an operator confirms the account is safe to release.

## Risk behavior

- `risk_pct` is a maximum for **all** sizing modes, not only percentage sizing. Its current validated maximum is 2% for every account, including small accounts.
- Percentage mode budgets `equity × risk_pct / 100`; cash mode uses the smaller of that cap and `cash_risk`; fixed mode uses `fixed_lots` only if its stop exposure fits the percentage cap.
- Cash values use **the account's reported currency units**. A cent account reports cent units; there is no implicit USD-to-cent conversion or currency conversion. The UI labels this behavior explicitly.
- Lot sizes round down to the broker step and max-lot cap. If the minimum lot would breach the budget, the trade is skipped. No auto-round-up.
- Existing positions and pending orders across the dedicated account count toward aggregate stop-loss risk and exposure limits. Existing exposure without a stop blocks new entries.
- Daily equity loss is measured from a persisted UTC daily baseline; high-water equity is persisted for drawdown checks. The worker reconstructs its initial daily baseline from current balance and today's trading deals. Deposits/withdrawals, prior unobserved floating peaks, and changes while offline require operator review; this is not a broker-enforced loss cap.
- These gates stop **new entries**. They do not auto-liquidate existing positions when a daily or drawdown limit is hit, and they cannot guarantee a maximum realized loss.
- Risk changes apply to future queued entries. Each signal's risk configuration is snapshotted at intake. Already queued/open entries retain their original settings; pause and review queued entries if you need immediate changes.

## Signal and management grammar

Only text signals for XAUUSD are supported. Configure the exact broker suffix independently for each account.

```text
XAUUSD BUY
ENTRY: 2700 - 2701
SL: 2695
TP1: 2710
TP2: 2720
BE: 2705
```

Use `XAUUSD BUY LIMIT`, `SELL LIMIT`, `BUY STOP`, or `SELL STOP` with an **exact** ENTRY for pending orders. A single target uses `TP:`. BUY requires `SL < entry < BE <= TP1 <= TP2` with distinct ordered targets; SELL reverses the order.

Referenced instructions use the original Discord entry message ID:

```text
BE SIGNAL: 123456789
```

```text
UPDATE SIGNAL: 123456789
SL: 2700
TP: 2725
```

```text
PARTIAL SIGNAL: 123456789
PERCENT: 50
```

`CLOSE SIGNAL: 123456789` closes matching open positions. `CANCEL SIGNAL: 123456789` cancels matching pending orders. Neither is interpreted as the other. The reference must belong to the same analyst feed. Invalid formats, images/embeds, ambiguous instructions, unrelated instruments, and unauthorized authors are ignored/rejected.

An analyst can be a human Discord user or a signal bot. In **Operator console / Add authorized analyst**, enter that author's user ID together with the exact server (guild) and channel IDs. Each member follows that analyst for each MT5 account and enables copying after the worker connects. The same authorization and signal parser apply to both author types. Unconfigured authors, webhooks, DMs, and messages from the listener itself are ignored. Bots must put the supported signal text in message content; embed/image-only signals are not parsed.

Orders follow this path: **Discord author -> configured analyst -> following, enabled accounts -> account worker queue -> Windows MT5 terminal -> broker**. The broker label (including Exness) does not select a website integration. Workers use the MetaTrader5 Python API, not the Exness website or browser automation. Broker/terminal trading permissions, risk limits, freshness, and operator gates still apply; a signal cannot force an order past those checks.

Discord text edits propagate only SL/final-TP changes to an already ingested entry. An entry-price/side/type edit never creates a second trade. Changes to TP1/BE/range semantics require a new explicit supported instruction; post deletion never means cancel/close.

## Trade management

BE uses the actual broker fill price and the exit-side quote, excluding fees and slippage. The worker preserves a tighter stop. TP allocations reference the original detected filled volume. For multiple targets, partial exits are journaled before submission; an uncertain exit is not automatically repeated. Allocations below broker minimum lot are recorded as skipped. A remainder smaller than broker minimum is rejected rather than silently liquidated.

A runner is the unallocated remainder after the configured TP allocations. A runner has no final broker TP, so its partial targets require the worker. Otherwise, the final broker TP is retained as an additional exit. Broker-side SL/TP survive a disconnected worker; software BE and partial targets require it to be running. Partial exits also require fresh service authorization; during service/network outages only idempotent local BE inspection continues. Revoked workers stop when authorization is rejected. Always stop and verify an old worker before transferring its account to a replacement.

With **Preserve manual SL/TP adjustments**, the worker detects changes against its last observed/applied stops, suppresses corresponding automatic changes, and preserves them across analyst update commands. Edits that occur before the worker first observes the newly opened position cannot reliably be distinguished from broker execution adjustments; verify this behavior during acceptance.

The frontend can request explicit BE, stop edits, partial/full close, and pending cancellation for an account's copier tickets. The worker checks symbol and copier magic. External/manual positions cannot be controlled by these endpoints. Account risk checks still include external exposure.

## Optional direct MT5 analyst mirroring

Configure a dedicated, already logged-in **master source** terminal, `MASTER_TERMINAL_PATH`, `MASTER_LOGIN`, `MASTER_SERVER`, `MASTER_SYMBOL`, `MIRROR_ANALYST_ID`, and `SIGNAL_INGEST_TOKEN`. The same ingest token must be configured on the server. Run `python -m goldcopier.master_bridge` on the master Windows host.

The bridge only reads the master. It baselines already-open positions at startup, mirrors newly opened positions with SL/TP, and emits referenced changes/partial/full-close events. BE is derived from the smaller of one stop-distance and half the target-distance; it is not an undocumented master BE setting. Master pending orders and scale-ins are not mirrored; use standard Discord signals for pending entries. Missing SL/TP or scale-ins require explicit operator review. Keep the bridge state file; do not run two instances for one analyst.

## Stripe subscriptions

Set `BILLING_ENABLED=true`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and the two recurring `STRIPE_PRICE_BASIC`/`STRIPE_PRICE_PRO` IDs. Configure the Stripe customer portal, permitted return URLs, pricing, taxation, and business details in your Stripe account.

Register **https://your-domain/api/billing/webhook** for subscription created/updated/deleted events. Test with Stripe test-mode keys first. Checkout never grants access from a browser redirect: signed webhook events are idempotently processed, and the server retrieves the current subscription to avoid stale event payloads. Active/trialing status plus a nonexpired period is required at start/intake/claim. Managed exit commands remain available when a subscription expires.

Basic and Pro currently expose the same software controls; differentiated account/analyst quotas and commercial plan packaging are not implemented. Stripe prices are never guessed or hard-coded in the frontend. Cryptocurrency billing is not implemented.

## Password resets

Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, and optional SMTP credentials. The service uses STARTTLS and one-time 30-minute reset tokens. A reset revokes all existing sessions. Reset requests respond generically for unknown addresses. Demo/production auth uses the same session code; no production default password is provided.

## Pause, recovery and backups

- Global pause cancels queued **entry** instructions and blocks new entry intake/claims. Account pause does the same for that account's queued entries. Already claimed submissions can still execute. Neither action closes exposure.
- A local `PAUSE` file blocks new entries while management continues.
- Atomic claims are not leased/redelivered. A lost claim response remains `claimed` for operator review.
- The local worker commits an intent before `order_send`. It stores the result before acknowledging the service. Network retries resend acknowledgements, never the order.
- An interrupted or nonfinal broker response becomes `uncertain`, pauses that account, and blocks further entries/automatic partials. Idempotent BE inspection continues.
- Review actual MT5 orders/deals/positions and the journal. In **Operator console → Reconcile**, record a verified outcome and broker evidence. This updates records; it never resubmits an order. The worker pulls these exact job resolutions to clear its local uncertainty. Stop a stale worker before resolving its submission.
- Rotate worker tokens through the UI to revoke a worker and pause copying. Disconnection removes stored credentials and tokens only when a fresh empty account snapshot is verified. Do not disconnect an account with live exposure.
- Do not delete journals, pool state, or baselines to bypass a block. Preserve them for recovery.

Create an online-consistent backup with `python -m goldcopier.manage backup --output /data/backup-YYYYMMDD.db`. Back up the encryption key separately. Test restore in an isolated installation with broker execution disabled. Vault-key rotation requires a planned decrypt/re-encrypt migration and coordinated worker/service restart; no automatic vault rekey workflow is provided.

## Acceptance checklist

Before enabling live execution on **both** server and worker, verify on broker demos: two independent followers, wrong-account rejection, symbol suffix and filling mode, entry types/expiry, volume min/step, cash/percentage/fixed caps, cent-account units, existing risk exposure, daily/drawdown stops, TP allocations/minimum remainders, manual stop preservation, BE on actual fill, disconnects, broker comment changes, partial fills, and restart at each side of submission/acknowledgement. Test signed Stripe webhooks and real test-mode checkout/portal, SMTP delivery, database restore, worker token revocation, and the managed pool under your Windows service manager. Test the frontend visually on desktop/mobile browsers.

There is no guarantee of identical fills, zero slippage, submillisecond execution, 99.99% availability, or profit. This implementation intentionally does not claim those marketing guarantees.
