"""Community subscriptions: verified entitlements and Stripe Connect destinations."""

import os
import time
import uuid

import stripe
from . import community as C


class CommunityBilling:
    def __init__(self, billing):
        self.billing = billing
        self.store = billing.store
        self.base = billing.base_url
        self.key = billing.key

    def ready(self):
        if not self.billing.enabled or not self.key or not self.billing.secret:
            raise ValueError(
                "Subscription payments are not configured by this service yet"
            )

    def onboard(self, user, cid):
        self.ready()
        with self.store.db() as db:
            c = C.owned(db, user["id"], cid)
        account = c["stripe_account"]
        if not account:
            result = stripe.Account.create(
                type="express",
                email=user["email"],
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
                metadata={"community_id": cid, "owner_id": user["id"]},
                api_key=self.key,
                idempotency_key="community-connect:" + cid,
            )
            account = result.id
            with self.store.db() as db:
                db.execute(
                    "UPDATE communities SET stripe_account=? WHERE id=?", (account, cid)
                )
        return stripe.AccountLink.create(
            account=account,
            refresh_url=self.base + "/#community",
            return_url=self.base + "/#community",
            type="account_onboarding",
            api_key=self.key,
        ).url

    def refresh(self, user, cid):
        self.ready()
        with self.store.db() as db:
            c = C.owned(db, user["id"], cid)
        if not c["stripe_account"]:
            return {"ready": False}
        account = stripe.Account.retrieve(c["stripe_account"], api_key=self.key)
        ready = bool(account.charges_enabled and account.payouts_enabled)
        with self.store.db() as db:
            db.execute(
                "UPDATE communities SET payments_ready=? WHERE id=?", (int(ready), cid)
            )
        return {"ready": ready}

    def checkout(self, user, cid):
        self.ready()
        with self.store.db() as db:
            c = db.execute(
                "SELECT * FROM communities WHERE id=? AND enabled=1", (cid,)
            ).fetchone()
            m = db.execute(
                "SELECT * FROM community_memberships WHERE community_id=? AND user_id=?",
                (cid, user["id"]),
            ).fetchone()
            if (
                not c
                or not m
                or c["access_mode"] != "stripe"
                or c["owner_id"] == user["id"]
            ):
                raise ValueError("Join this master using their invite link first")
            if not C.master_licensed(db, c["owner_id"], True):
                raise ValueError("This master subscription is not currently available")
            if C.member_active(m):
                raise ValueError("Your subscription is already active")
            if m["blocked"]:
                raise ValueError("The master has paused your membership")
            c, m = dict(c), dict(m)
        if not c["stripe_account"]:
            raise ValueError(
                "The master must finish payment setup before subscriptions can be sold"
            )
        destination = stripe.Account.retrieve(c["stripe_account"], api_key=self.key)
        if not destination.charges_enabled or not destination.payouts_enabled:
            raise ValueError("The master is still completing payment activation")
        if m["checkout_id"]:
            previous = stripe.checkout.Session.retrieve(
                m["checkout_id"], api_key=self.key
            )
            if previous.status == "open":
                return previous.url
            if previous.status == "complete":
                raise ValueError(
                    "Payment is being verified. Your bot will appear when verification completes."
                )
            m["checkout_key"] = uuid.uuid4().hex
            with self.store.db() as db:
                db.execute(
                    "UPDATE community_memberships SET checkout_key=?,checkout_id=NULL,checkout_url=NULL WHERE community_id=? AND user_id=?",
                    (m["checkout_key"], cid, user["id"]),
                )
        customer = m["customer"]
        if not customer:
            customer = stripe.Customer.create(
                email=user["email"],
                metadata={"community_id": cid, "user_id": user["id"]},
                api_key=self.key,
                idempotency_key="community-customer:" + cid + ":" + user["id"],
            ).id
            with self.store.db() as db:
                db.execute(
                    "UPDATE community_memberships SET customer=? WHERE community_id=? AND user_id=?",
                    (customer, cid, user["id"]),
                )
        metadata = {
            "community_id": cid,
            "user_id": user["id"],
            "owner_id": c["owner_id"],
        }
        subscription = {
            "metadata": metadata,
            "transfer_data": {"destination": c["stripe_account"]},
        }
        fee = float(os.environ.get("COMMUNITY_APPLICATION_FEE_PERCENT", "0"))
        if not 0 <= fee <= 100:
            raise ValueError("Invalid service payment configuration")
        if fee:
            subscription["application_fee_percent"] = fee
        checkout = stripe.checkout.Session.create(
            customer=customer,
            mode="subscription",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": m["expected_cents"],
                        "recurring": {"interval": "month"},
                        "product_data": {"name": c["name"] + " copy bot subscription"},
                    },
                    "quantity": 1,
                }
            ],
            metadata=metadata,
            subscription_data=subscription,
            success_url=self.base + "/#community",
            cancel_url=self.base + "/#community",
            api_key=self.key,
            idempotency_key="community-checkout:" + m["checkout_key"],
        )
        with self.store.db() as db:
            db.execute(
                "UPDATE community_memberships SET checkout_id=?,checkout_url=? WHERE community_id=? AND user_id=?",
                (checkout.id, checkout.url, cid, user["id"]),
            )
        return checkout.url

    def portal(self, user, cid):
        self.ready()
        with self.store.db() as db:
            m = db.execute(
                "SELECT customer FROM community_memberships WHERE community_id=? AND user_id=?",
                (cid, user["id"]),
            ).fetchone()
        if not m or not m["customer"]:
            raise ValueError("No subscription billing account exists for this master")
        return stripe.billing_portal.Session.create(
            customer=m["customer"],
            return_url=self.base + "/#community",
            api_key=self.key,
        ).url

    def handle(self, event):
        if not event.type.startswith("customer.subscription."):
            return False
        metadata = event.data.object.get("metadata") or {}
        if not metadata.get("community_id"):
            return False
        with self.store.db() as db:
            if db.execute(
                "SELECT 1 FROM webhook_events WHERE id=?", (event.id,)
            ).fetchone():
                return True
        current = stripe.Subscription.retrieve(event.data.object.id, api_key=self.key)
        metadata = current.get("metadata") or {}
        cid, uid = metadata.get("community_id"), metadata.get("user_id")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM webhook_events WHERE id=?", (event.id,)
            ).fetchone():
                return True
            c = db.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone()
            m = db.execute(
                "SELECT * FROM community_memberships WHERE community_id=? AND user_id=?",
                (cid, uid),
            ).fetchone()
            if (
                not c
                or not m
                or c["access_mode"] != "stripe"
                or metadata.get("owner_id") != c["owner_id"]
                or current.customer != m["customer"]
            ):
                raise ValueError("Payment is not linked to this master and subscriber")
            items = current["items"]["data"]
            price = items[0]["price"] if len(items) == 1 else {}
            destination = (current.get("transfer_data") or {}).get("destination")
            if (
                price.get("unit_amount") != m["expected_cents"]
                or price.get("currency") != "usd"
                or (price.get("recurring") or {}).get("interval") != "month"
                or destination != c["stripe_account"]
            ):
                raise ValueError(
                    "Subscription price or master payout destination is invalid"
                )
            end = current.get("current_period_end") or max(
                (i.get("current_period_end", 0) for i in items), default=0
            )
            if m["billing_subscription"] and m["billing_subscription"] != current.id:
                db.execute(
                    "INSERT INTO webhook_events VALUES(?,?)", (event.id, event.created)
                )
                return True
            db.execute(
                "UPDATE community_memberships SET billing_subscription=?,status=?,period_end=? WHERE community_id=? AND user_id=?",
                (current.id, current.status, end, cid, uid),
            )
            if (
                current.status in ("active", "trialing")
                and end > time.time()
                and not m["blocked"]
            ):
                C.ensure_bot(db, cid, uid)
                db.execute(
                    "UPDATE community_bots SET auto_start=desired_enabled WHERE community_id=? AND user_id=?",
                    (cid, uid),
                )
            else:
                C.pause_bots(db, cid, uid)
                if current.status in ("canceled", "incomplete_expired"):
                    db.execute(
                        "UPDATE community_memberships SET billing_subscription=NULL,checkout_id=NULL,checkout_url=NULL,checkout_key=? WHERE community_id=? AND user_id=?",
                        (uuid.uuid4().hex, cid, uid),
                    )
            db.execute(
                "INSERT INTO webhook_events VALUES(?,?)", (event.id, event.created)
            )
            self.store.event(
                db,
                uid,
                "subscription",
                "Master subscription verified: " + current.status,
            )
        return True
