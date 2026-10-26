# Gold Copier — community trading platform

A full-stack XAUUSD community copier with a responsive member dashboard, browser authentication, individual MT5 accounts, analyst feeds, risk controls, trade management, subscriptions, and a managed Windows terminal pool.

**This branch is `feature/community-platform`. `main` retains the original backend-only MVP.** The interface is an original implementation of the public feature set, not a copy of Inspired Analyst's private dashboard or source code.

## Where is the frontend?

The frontend is in [`goldcopier/web/`](goldcopier/web/): HTML, CSS, and JavaScript served directly by FastAPI. There is no separate frontend build or Node runtime needed to run the application. Node is used only for frontend tests.

- Open [`docs/frontend-preview.html`](docs/frontend-preview.html) after downloading it for a self-contained, read-only preview. Select **Explore live demo** on the landing page.
- For the actual running app, start the local demo below and open **http://127.0.0.1:8000/#demo**.
- Optional operator sign-in uses `demo@goldcopier.local`; the launcher stores its generated password in `.demo/access-password`. Demo data are explicitly labelled and **no broker jobs can be claimed in demo mode**.

## Start the frontend and backend together

Python 3.11 or 3.12:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.lock.txt
pip install -e . --no-deps
python -m goldcopier.launch --demo
```

Open **http://127.0.0.1:8000/** for the landing page, **/#demo** for the read-only sample workspace, or **/#login** to sign in and edit demo settings. Demo initialization uses a separate `.demo` state directory. It does not connect to a broker, require broker credentials, or enable payments.

## Included

| Area | Implementation |
| --- | --- |
| Frontend | Landing page, signup/login/password reset, dashboard, account wizard, analysts, risk settings, trade controls/history, activity, billing, settings, operator console |
| Account security | Scrypt password hashes; hashed opaque session/worker tokens; HttpOnly/SameSite cookies; CSRF and Origin checks; login throttling; AES-256-GCM encryption of stored MT5 passwords |
| Account onboarding | Self-hosted or managed terminal assignment, explicit account-owner consent, worker verification, per-account token rotation and credential revocation |
| Signals | XAUUSD market/limit/stop orders, exact entry or market entry ranges, single or multiple targets, BE, referenced close/cancel/partial/update commands, authorized Discord text edits |
| Risk | Percentage, account-currency cash, or fixed-lot sizing; percentage/lot caps; daily equity loss stop; persisted high-water drawdown; aggregate exposure and concurrent-position limits; spread and entry filters |
| Management | Actual-fill BE; durable partial-exit stages; TP1/TP2 and runners; manual SL/TP adjustment protection; pending cancellation/modification; broker position reconciliation by comment or order history |
| Reliability | Atomic single claims, durable local submission journals, acknowledgement retry, uncertainty blocks, operator reconciliation without order replay, worker heartbeats and activity alerts |
| Billing | Stripe Checkout, billing portal, signed/idempotent subscription webhooks, authoritative subscription retrieval, entitlement checks at intake/start/claim |
| Managed execution | Dedicated Windows MT5 terminal pool, one worker per account, encrypted credential provisioning, persistent account-to-slot assignment |
| Analyst sources | Authorized Discord feeds; optional read-only MT5 master-position bridge |
| Deployment | Non-root Docker image, Compose, Caddy HTTPS example, database backups, supervised recovery instructions |

## Verification

```bash
pytest -q
npm ci --ignore-scripts
npm test
```

Python tests cover account isolation, authentication/CSRF, vault binding, webhook signatures and entitlements, signal fanout, demo/live gates, broker guards, partial exits, manual stop preservation, duplicate prevention, interrupted submissions, and reconciliation. DOM tests exercise the real frontend source, navigation, account wizard, risk-form payloads, trade review, read-only demo controls, and HTML escaping. The preview embeds labelled sample data; it does not replace the authenticated API in the real application.

**Actual MT5/Windows execution, Stripe test-mode checkout, SMTP delivery, Docker deployment, and browser visual/responsive QA still require environment-specific acceptance.** Automated broker tests use a fake MT5 API. Do not interpret passing tests as broker certification or a production uptime guarantee.

## Deployment and operation

See [`docs/PLATFORM.md`](docs/PLATFORM.md) for production setup, worker/pool configuration, billing, Discord grammar, risk semantics, and recovery. See [`docs/FEATURES.md`](docs/FEATURES.md) for the explicit feature comparison and remaining limits. The original v0.1 setup is preserved in [`docs/LEGACY_MVP.md`](docs/LEGACY_MVP.md); new installations should use the platform app rather than the legacy server.

The service is not deployed by committing this branch. Live execution is disabled by default and requires **both** server and worker enablement, explicit account consent, broker demo acceptance, and operator-managed infrastructure. Only MT5 accounts are supported; MT4 and PrimeXBT PXTrader are separate integrations.
