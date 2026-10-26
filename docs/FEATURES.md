# Public-feature comparison and delivery status

This implementation uses the public descriptions of Inspired Analyst Bot as a functional reference. The private signed-in platform and its source have not been inspected. UI design, branding, and copy are original. This is not a verified byte-for-byte or feature-for-feature clone.

| Publicly described capability | This branch |
| --- | --- |
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
| Direct analyst MT5 mirroring | Read-only master-position bridge implemented; master pending orders/scale-ins excluded |
| Pending follower entries | Supported through Discord LIMIT/STOP signals |
| Hot standby / zero-downtime claims | Not implemented as an automatic trading failover mechanism; supervised recovery documented |
| P&L and trade visibility | Snapshot equity chart, current positions/orders, today's closed deals, realized P&L, command history and CSV |
| Multiple instruments | XAUUSD only, as originally requested |
| Broker compatibility | MT5 adapter; Exness/PrimeXBT MT5 require account-specific testing; PXTrader/MT4 unsupported |
| Public commercial readiness | Not represented as complete: broker validation, payment acceptance, security review, legal/business configuration and deployment are required |

The local demo is a real runnable application with labelled sample records and broker claims disabled. The standalone HTML preview uses a read-only data adapter. Neither implies that a live broker or payment provider has been connected.

Automated tests verify application behavior with fake broker/provider responses. They do not certify Windows/MT5 behavior, broker execution, infrastructure uptime, or payment-provider approval.
