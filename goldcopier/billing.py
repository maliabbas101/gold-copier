"""Stripe adapter. Entitlements come from verified webhook + current subscription state."""

import os
import time
import stripe


class Billing:
    def __init__(self, store, base_url, enabled):
        self.store, self.base_url, self.enabled = store, base_url, enabled
        self.key = os.environ.get("STRIPE_SECRET_KEY", "")
        self.secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        self.prices = {
            p: os.environ.get("STRIPE_PRICE_" + p.upper(), "") for p in ("basic", "pro")
        }

    def ready(self):
        if (
            not self.enabled
            or not self.key
            or not self.secret
            or not all(self.prices.values())
        ):
            raise ValueError("Billing is not configured by the operator")

    def checkout(self, user, plan):
        self.ready()
        with self.store.db() as db:
            row = db.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (user["id"],)
            ).fetchone()
            customer = row["customer"]
            if row["status"] in ("active", "trialing"):
                raise ValueError(
                    "Manage your existing subscription in the billing portal"
                )
        if not customer:
            c = stripe.Customer.create(
                email=user["email"],
                metadata={"user_id": user["id"]},
                api_key=self.key,
                idempotency_key="customer:" + user["id"],
            )
            customer = c.id
            with self.store.db() as db:
                db.execute(
                    "UPDATE subscriptions SET customer=? WHERE user_id=?",
                    (customer, user["id"]),
                )
        checkout = stripe.checkout.Session.create(
            customer=customer,
            mode="subscription",
            line_items=[{"price": self.prices[plan], "quantity": 1}],
            success_url=self.base_url + "/#billing",
            cancel_url=self.base_url + "/#billing",
            subscription_data={"metadata": {"user_id": user["id"]}},
            api_key=self.key,
        )
        return checkout.url

    def portal(self, user):
        self.ready()
        with self.store.db() as db:
            c = db.execute(
                "SELECT customer FROM subscriptions WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
        if not c:
            raise ValueError("No billing customer exists yet")
        return stripe.billing_portal.Session.create(
            customer=c, return_url=self.base_url + "/#billing", api_key=self.key
        ).url

    def webhook(self, payload, signature):
        self.ready()
        event = stripe.Webhook.construct_event(payload, signature, self.secret)
        with self.store.db() as db:
            if db.execute(
                "SELECT 1 FROM webhook_events WHERE id=?", (event.id,)
            ).fetchone():
                return
        if event.type not in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            return
        obj = event.data.object
        # Retrieve current state to avoid out-of-order webhooks granting stale access.
        current = stripe.Subscription.retrieve(obj.id, api_key=self.key)
        customer = current.customer
        items = current["items"]["data"]
        price = items[0]["price"]["id"] if items else ""
        plan = next((p for p, v in self.prices.items() if v == price), "free")
        end = current.get("current_period_end") or max(
            (i.get("current_period_end", 0) for i in items), default=0
        )
        status = current.status if plan != "free" else "inactive"
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM webhook_events WHERE id=?", (event.id,)
            ).fetchone():
                return
            row = db.execute(
                "SELECT user_id FROM subscriptions WHERE customer=?", (customer,)
            ).fetchone()
            if not row:
                raise ValueError("Webhook customer is not linked to this platform")
            db.execute(
                "UPDATE subscriptions SET subscription=?,plan=?,status=?,period_end=?,updated=? WHERE customer=?",
                (current.id, plan, status, end, time.time(), customer),
            )
            db.execute(
                "INSERT INTO webhook_events VALUES(?,?)", (event.id, event.created)
            )
            self.store.event(
                db, row["user_id"], "billing", "Subscription status: " + status
            )
