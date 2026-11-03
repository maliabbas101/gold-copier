# Master communities and personal copy bots

## What A and B do

A is a master trader who purchases the platform's Pro software subscription. In **Community**, A creates a community, connects their master broker account once, chooses a Discord output channel, and shares their subscriber invite. **Connect Discord** uses Discord's hosted authorization page and verifies that A owns or manages the destination server. The shared output bot cannot be pointed at an unrelated server just by pasting its channel ID.

B opens A's invitation and creates a personal login. In paid communities B subscribes through hosted checkout; in approval communities A explicitly grants access. Once access is active, a private child bot is generated. B connects their own broker account once, chooses their risk settings, and the hosted worker starts copying after a healthy broker connection. A sees membership and connection status, not B's broker credentials. Each child has a separate account token, execution ledger, and broker session; it is an isolated running worker, not a downloadable bot build.

A continues trading in their normal MT5 terminal. The parent observer detects a new protected fill and queues child trades. Once at least one follower confirms successful execution, the server queues a Discord trade signal. Trades opened through the Exness app/web use this same path when they appear in the connected master account. If no follower executes, there is no Discord trade signal; skipped/rejected reasons appear in private Activity. A confirmation does not imply every follower filled. Discord is an output destination, so a Discord outage does not hold up copying. Neither A nor B retypes the trade into an operator form. CMP entries are priced using the follower broker's executable quote; no current-price number is required in a manual CMP message.

“Same trade” means the same direction and mapped instrument, with each child's configured sizing and execution limits. Spread, available symbols, market hours, broker acceptance, and connection delays can change fills or prevent a child entry. Existing positions at connection time are baselined rather than copied. A source trade needs a mapped symbol and SL. Source pending orders are copied when filled; scale-ins to an existing source position are not separately mirrored. See [README](../README.md#what-is-copied-and-what-is-not) for observation and management limits.

## Access and billing

- The platform's software subscription belongs to A. With billing enabled, a current Pro subscription is required; the service operator can grant an explicit administrative entitlement. Billing-disabled development does not require a payment.
- A chooses an approval community or a monthly USD subscription community at creation. Approval creates a time-limited `granted` membership; it does not assert that payment was collected.
- Paid communities onboard A through Stripe Connect Express. B's checkout creates a subscription whose funds transfer to A's connected account. `COMMUNITY_APPLICATION_FEE_PERCENT` defaults to zero. Provider fees and account eligibility still apply.
- Checkout redirects never grant access. A verified, signed webhook and an authoritative provider subscription read must agree on the community, customer, monthly amount, currency, and destination. Duplicate events are ignored.
- An active membership creates a private child bot record. A can pause a member but cannot use the approval action to manufacture a paid entitlement in paid mode. B can open their own hosted billing portal.
- Pausing, expiration, or cancellation blocks new entries at ingestion and worker claim. Existing copied positions can still receive management and close actions while their worker remains connected. Pausing does not liquidate broker positions.

## One-time service deployment

These are tasks for the software service operator, not steps in A/B onboarding. The web UI does not provision a cloud server or Windows VM itself.

1. Run the authenticated platform behind HTTPS with persistent database, stable `VAULT_KEY`, backups, and the existing live-trading gate. Use a real `PUBLIC_URL`; it is used for invites and hosted payment/OAuth returns.
2. Configure `BILLING_ENABLED`, Stripe secret/webhook keys and `STRIPE_PRICE_BASIC` / `STRIPE_PRICE_PRO` for platform licenses. Configure Stripe Connect for the platform account and send `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted` to `/api/billing/webhook`. Enable the hosted customer portal. Use test mode before production.
3. Configure `DISCORD_APPLICATION_ID`, `DISCORD_CLIENT_SECRET`, and `DISCORD_BOT_TOKEN` for a service-owned Discord application. Register the exact redirect URI `https://YOUR_DOMAIN/api/community-discord/callback`. Allow guild bot installation. The master authorizes `identify guilds bot`; the requested bot permissions are View Channel and Send Messages. Channel overrides must also permit output. Keep the existing Discord/outbox service running (the standard runtime runs it).
4. Configure a strong shared `SUPERVISOR_TOKEN` on the platform and worker host, plus `COPIER_URL` on the host. Start `python -m goldcopier.supervisor` as a monitored service. For MT5, supply `TERMINAL_POOL` pointing to JSON shaped as `{"terminals":["C:\\MT5-01\\terminal64.exe","C:\\MT5-02\\terminal64.exe"]}`. Each concurrent MT5 parent or child requires a distinct Windows terminal installation. Size capacity for the master plus all connected followers. No shared terminal between accounts.
5. The supervisor assigns managed accounts and starts separate account-scoped workers. Native eligible Exness API accounts use `managed_api` and need no MT5 terminal slot; API-only hosts may omit the terminal pool file. Standard Exness MT5 accounts should use MT5. Exness native API access is eligibility-dependent, not a universal retail API entitlement.
6. Run the full A/B flow with demo broker accounts: A connects, B joins and subscribes/gets approved, B connects, a new A fill with SL queues a child entry and Discord log, then SL/TP changes and close propagate. Check connection errors, expiry, and disconnect behavior. Mock tests do not replace this acceptance check.

A missing supervisor, full terminal pool, unavailable broker API, or missing payment/Discord configuration is reported rather than treated as a successful connection. Service secrets stay on the platform/host. Child subprocess environments strip parent, Discord, vault, and payment secrets.

## Implementation

`community.py` owns the parent/membership/child model and entitlement checks. `community_routes.py` exposes scoped onboarding APIs; `community_billing.py` handles hosted subscriptions; `community_discord.py` binds an authorized Discord destination to its master. Legacy Discord-ingestion/operator tools remain for advanced installations; they are not part of the default community onboarding.

One community is currently supported per master login and one connected child account per member per community. Pair CRUD and per-account symbol maps remain available. Membership cancellation does not remove stored history. Broker account disconnect/delete remain explicit actions on account cards.
