# Public-feature comparison and delivery status

This implementation uses the public descriptions of Inspired Analyst Bot as a functional reference. The private signed-in platform and its source have not been inspected. UI design, branding, and copy are original. This is not a verified byte-for-byte or feature-for-feature clone.

| Publicly described capability | This branch |
| --- | --- |
| Master → subscriber product | Community onboarding, scoped master dashboard, invite signup, subscription access, generated private child bots; see [deployment](COMMUNITIES.md) |
| Master subscriber billing | Stripe Connect hosted onboarding/checkout/portal with signed webhook verification; live provider setup required |
| Authorized Discord output | Master authorizes a server they manage through OAuth; copying runs independently of Discord output |
| Website, sign-in, member dashboard | Implemented; bundled frontend and authenticated API |
| Connect individual MT5 accounts | Implemented; consent, verification, token isolation, managed or self-hosted worker |
| Cloud account execution without member VPS setup | Managed terminal pool implemented; operator must supply and supervise Windows capacity |
| Analyst selection | Multiple allowlisted feeds and account-specific follows; switching/unfollow is blocked with existing exposure |
| Per-analyst risk | Account defaults plus analyst override API/UI |
| Percentage / dollar / fixed-lot risk | Percentage, account-currency cash, and fixed lots; explicit account-unit semantics rather than automatic USD/cent conversion |
| TP1 / TP2 / runners / BE | Implemented in the worker; broker-specific acceptance remains required |
| Manual SL/TP controls | Implemented with explicit command review and adjustment preservation |
| Small-account protection | All accounts have a maximum 2% per-trade setting; lot-minimum trades that exceed budget are skipped |
| Smart cent-account recommendations | Not implemented; broker account/currency is reported, cash units are labelled explicitly |
| Card subscriptions / cancellation portal | Stripe integration implemented; operator keys, products, portal and webhooks required |
| Crypto payments | Not implemented |
| Secure stored credentials | Server-side AES-256-GCM at-rest vault; no claim of client-side/end-to-end encryption |
| Direct trader copying | Scoped read-only MT5/eligible Exness source worker → follower queue; asynchronous Discord outbox; source pending orders and scale-ins excluded |
| Pending follower entries | Explicit LIMIT/STOP and AUTO; CMP executes at current market within tolerance |
| Hot standby / zero-downtime claims | Not implemented as an automatic trading failover mechanism; supervised recovery documented |
| P&L and trade visibility | Snapshot equity chart, current positions/orders, today's closed deals, realized P&L, command history and CSV |
| Multiple instruments | Configurable canonical pair directory, aliases, per-account broker mappings and limits; multi-signal intake |
| Broker compatibility | MT5 plus the official Exness signed API connector for eligible accounts; acceptance testing still required; PXTrader/MT4 unsupported |
| Public commercial readiness | Not represented as complete: broker validation, payment acceptance, security review, legal/business configuration and deployment are required |

The local demo is a real runnable application with labelled sample records and broker claims disabled. The standalone HTML preview uses a read-only data adapter. Neither implies that a live broker or payment provider has been connected.

Automated tests verify application behavior with fake broker/provider responses. They do not certify Windows/MT5 behavior, broker execution, infrastructure uptime, or payment-provider approval.

The multi-instrument / direct API update supersedes the original gold-only limitations. See [INSTRUMENTS_AND_EXNESS.md](INSTRUMENTS_AND_EXNESS.md).

Trader APP templates, optional later TP/BE, half-risk split entries, referenced/reply updates and informational status messages are covered by [README examples](../README.md#trader-signal-formats-cmp-and-later-updates).

Live portfolio: one-second authenticated browser stream with polling fallback, broker snapshot/current-price updates, a rolling live equity chart, account-specific history, stale-data indicators, and preservation of open dialogs/search. See [live portfolio setup](../README.md#live-portfolio-and-trade-updates).

CMP supports no-price `BUY XAUUSD CMP` and `ENTRY: CMP`. Operator console signal entry is an optional manual-test path; it is not required after a Discord post or broker fill.
