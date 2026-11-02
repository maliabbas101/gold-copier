> Trader alert/CMP update: see [README signal examples and missed-entry diagnostics](../README.md#trader-signal-formats-cmp-and-later-updates). Untyped entries now use AUTO; TP/BE may be supplied later.

# Multiple instruments and the official Exness API

Implemented on `feature/community-platform`. The existing MT5 path remains supported; Exness direct API accounts are a separate connector.

## Pair setup in the frontend

1. As an operator, open **Trading pairs → Add pair**. Enter the canonical symbol (e.g. EURUSD), display name, and optional exact aliases (e.g. EURO). Canonical symbols cannot be renamed; aliases must be unique across pairs.
2. Select a follower account and click **Map a pair**. Select the canonical pair, exact broker symbol (e.g. EURUSDm), maximum spread, entry tolerance, and lot cap. Verified broker instruments are offered as suggestions after a worker heartbeat.
3. Repeat for any instruments available on that account. Limits use raw instrument price units, never an inferred pip size. The account/analyst risk cap and account-wide aggregate exposure limits still apply; the lower account or mapping lot cap wins.
4. Edit, disable, or delete through the same page. Active exposure or queued/uncertain work blocks remapping/deletion. Management of an existing trade keeps its original broker instrument.

An unmapped pair is skipped for that follower and reported in Activity and the intake result. Unknown/disabled community symbols reject the message. No automatic symbol suffix matching, currency conversion guesses, or reuse of gold limits for a newly configured forex pair.

Existing accounts receive an initial gold mapping using their existing symbol and limits. This migration runs once. Deletions remain deleted after restart. Other instruments require explicit mappings. Back up the database before upgrading and restart both service and workers together; old workers are not multi-instrument aware.

## Multiple signals

One signal per message continues to work. A single Discord message can contain several complete signals separated by a line containing `---`, or a blank line before the next instrument header:

```text
XAUUSD BUY
ENTRY: 2700
SL: 2695
TP: 2710
BE: 2705
---
EURUSD BUY
ENTRY: 1.08001
SL: 1.07801
TP: 1.08401
BE: 1.08201
```

These are syntax examples, not current market signals. Prices support up to ten decimal places. All blocks are parsed/validated before anything is queued. At most 20 blocks are accepted per message.

Each block gets a unique trade reference. A batch from Discord message `123456` uses `123456-1`, `123456-2`, etc. For example, `BE SIGNAL: 123456-2` affects only the second signal. A single-signal message retains its original reference. For edits to batches, post explicit referenced management commands; editing the whole batch is not interpreted as a replacement portfolio.

Repeated delivery of the same message ID is ignored. Two different messages with identical prices are distinct intended signals and are both queued. Workers drain up to eight eligible instructions per cycle, execute serially per account, and reassess broker exposure before each entry. Separate signals on the same pair have separate copier references on MT5 hedging accounts. Position and aggregate risk limits can still reject later signals; those rejections appear in execution history. This is not unlimited execution or a guarantee of fills.

## Official Exness API connection

Exness currently publishes a signed Trader API using REST plus WebSocket events. Its official help explicitly says standard MetaTrader accounts cannot connect to the direct API. Exness API eligibility and available scopes must be verified in your Personal Area. Do not substitute an Exness website password or an MT5 investor password for API credentials.

1. Create the API key in Exness Key Management. Supply the needed account/trading scopes and any source-IP restrictions there. The application does not create API keys or accept API terms for you.
2. Copy the actual Gateway URL from Exness's DNS widget. Do not use a hostname copied from an example. The connector discovers and caches the account-specific Access Point.
3. In **Accounts → Exness API**, enter your account ID, key identifier, Gateway, and either the generated 128-hex Ed25519 private key or an Ed25519 PKCS#8 PEM. Credentials are encrypted with the existing account-bound AES-256-GCM vault and are only returned to that account's authenticated worker. They never appear in dashboards or journals.
4. Open **Worker setup**, generate its token, and run `python -m goldcopier.platform_agent` with `COPIER_URL` and `AGENT_TOKEN`. No MT5 terminal or Windows is needed for this connector. Use a dedicated `WORKER_STATE` file/directory per account.
5. Start with `ALLOW_LIVE_TRADING=false` on the server and worker. The worker can verify account details, balance, and the instrument catalogue while trading remains disabled. Direct API accounts are treated as live; the API account-details schema does not establish a demo-account identity.
6. Configure pairs, follow the trader, and complete broker acceptance. Actual execution requires explicit live enablement on both server and worker, plus Start copying. No credentials or live broker execution were used during development.

The API supports its own available instruments; Exness currently excludes stocks and indices from the direct API. MT5 instruments depend on the account and broker. For PrimeXBT and other MT5 accounts, continue using the MT5 worker.

## Execution and confirmation

- Ed25519 signing covers the exact method, path/query, body hash, millisecond timestamp and idempotency key. Secret keys support Exness's seed+public hex format and PEM; malformed/mismatched keys fail before network requests.
- Only HTTPS Exness-owned hosts are accepted. Signed REST and WebSocket requests must not follow redirects. Error reporting omits request credentials and signed headers. API 429 responses enforce a local cooldown; a mutating request is never blindly retried.
- Trading ACKs are not fills. Operation IDs are persisted, then the adapter checks final operation status and correlated transaction events. Missing/ambiguous confirmations become uncertain and block further entries until reconciliation.
- Broker position/order ownership is persisted from correlated events. External positions are never adopted just because the instrument matches. Foreign exposure on the same instrument blocks an entry. Reused/aggregated position IDs are treated as uncertain rather than reassigned to another signal; confirm the account's position behavior before allowing overlapping API signals.
- The existing sizing, exposure, BE, target, runner, and control engine is reused. Exness cash P&L conversion requires an available direct or inverse conversion instrument with a fresh quote; unavailable calculation/conversion metadata fails closed. Native instrument contract/volume/precision data comes from the API.
- SL/TP changes use API SLTP orders. The public API does not expose an MT5-style `order_check`; local guards run first, and Exness performs final margin/session/HMR/trading-rule validation asynchronously.
- Native API pending orders use its published order schema. The MT5 one-hour pending expiration field is not sent to Exness because the direct API schema has no expiration field; operators must explicitly manage/cancel API pending orders.
- A missing entity correlation, API stream gap, too much daily history for the bounded read, or unsupported metadata produces a visible block rather than guessed ownership or risk. Exness deployment quotas, network failures, API permissions, and live contract behavior require acceptance on your actual eligible account.

## API routes

| Purpose | Routes |
| --- | --- |
| Community pair directory | `GET /api/pairs`; operator `POST /api/operator/pairs`, `PUT/DELETE /api/operator/pairs/{symbol}` |
| Account mapping CRUD | `GET/POST /api/accounts/{id}/pairs`; `PUT/DELETE /api/accounts/{id}/pairs/{pair}` |
| Direct Exness account | `POST /api/accounts/exness` |
| Worker provisioning/status | Existing account token, configuration, heartbeat, claim, result, and reconciliation routes |

Mutations use the existing browser-session CSRF protection and account ownership checks. Community directory changes require operator rights.

## Official references

- [Exness API availability and eligibility](https://get.exness.help/hc/en-us/articles/27866287512476-Exness-API)
- [Key management and host setup](https://www.exness-api.com/authentication/api-key-and-host-setup)
- [Signing protocol](https://www.exness-api.com/authentication/request-signing)
- [Access Point discovery](https://www.exness-api.com/trading/get-started/access-point-host-discovery)
- [Official OpenAPI contract](https://www.exness-api.com/openapi/trading.public.yaml)
- [Asynchronous first-trade flow](https://www.exness-api.com/trading/get-started/place-first-trade)
- [WebSocket protocol](https://www.exness-api.com/trading/get-started/subscribe-to-websocket-events)

## Disconnecting and deleting an account

Both actions are available directly on each **Accounts** card. Disconnect pauses copying, cancels queued work, revokes the token, erases stored credentials, and prevents the local runner from automatically reconnecting. It works with an offline or broken broker connection and does not require adding another account or getting a fresh snapshot. Confirm that you will manage broker trades yourself: existing positions/orders remain at the broker, automated BE/targets stop, and an already submitted command may still execute. In-flight work is marked uncertain for reconciliation.

Delete also removes the account card, follows, and pair mappings. Reconcile claimed/uncertain work first; Disconnect remains available while you do so. Deleting the local account suppresses terminal bootstrap so it stays deleted after a service restart. Security events and execution records remain in the database for audit. To reconnect through the frontend, delete the disconnected account and add it again; supply managed/API credentials again because they were erased. A self-hosted worker can also be reauthorized through the account token endpoint.

The authenticated routes are `POST /api/accounts/{id}/disconnect` and `DELETE /api/accounts/{id}`. Both require CSRF protection and JSON `{"acknowledge_unmanaged_trades":true}`. The delete route now deletes the local account instead of acting as a disconnect alias.
