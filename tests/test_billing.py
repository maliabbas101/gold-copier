import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
import stripe

from goldcopier.billing import Billing
from goldcopier.platform_store import PlatformStore


def signed(payload, secret):
    ts = int(time.time())
    signature = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={signature}"


def test_stripe_signature_dedup_and_authoritative_state(tmp_path):
    store = PlatformStore(tmp_path / "billing.db")
    user = store.signup("Member", "member@example.com", "test-password-long")
    with store.db() as db:
        db.execute(
            "UPDATE subscriptions SET customer=? WHERE user_id=?", ("cus_owned", user)
        )
    billing = Billing(store, "https://copier.example", True)
    billing.key = "sk_test_placeholder"
    billing.secret = "whsec_testing"
    billing.prices = {"basic": "price_basic", "pro": "price_pro"}
    event = {
        "id": "evt_1",
        "object": "event",
        "type": "customer.subscription.updated",
        "created": int(time.time()),
        "data": {
            "object": {"id": "sub_1", "object": "subscription", "customer": "cus_owned"}
        },
    }
    payload = json.dumps(event).encode()
    current = stripe.StripeObject.construct_from(
        {
            "id": "sub_1",
            "customer": "cus_owned",
            "status": "active",
            "current_period_end": int(time.time() + 86400),
            "items": {"data": [{"price": {"id": "price_basic"}}]},
        },
        None,
    )
    with patch.object(
        stripe.Subscription, "retrieve", return_value=current
    ) as retrieve:
        billing.webhook(payload, signed(payload, billing.secret))
        billing.webhook(payload, signed(payload, billing.secret))
        assert retrieve.call_count == 1
    assert store.dashboard(user)["subscription"]["status"] == "active"
    # Even an older event uses the currently canceled subscription, never its stale embedded status.
    event["id"] = "evt_old"
    event["created"] -= 60
    payload = json.dumps(event).encode()
    current["status"] = "canceled"
    with patch.object(stripe.Subscription, "retrieve", return_value=current):
        billing.webhook(payload, signed(payload, billing.secret))
    assert store.dashboard(user)["subscription"]["status"] == "canceled"
    with pytest.raises(Exception):
        billing.webhook(payload, "t=1,v1=fake")


def test_unlinked_customer_cannot_grant_entitlement(tmp_path):
    store = PlatformStore(tmp_path / "billing.db")
    billing = Billing(store, "https://copier.example", True)
    billing.key = "test"
    billing.secret = "secret"
    billing.prices = {"basic": "basic", "pro": "pro"}
    event = {
        "id": "evt_fake",
        "object": "event",
        "type": "customer.subscription.updated",
        "created": int(time.time()),
        "data": {"object": {"id": "sub_1", "object": "subscription"}},
    }
    payload = json.dumps(event).encode()
    current = stripe.StripeObject.construct_from(
        {
            "id": "sub_1",
            "customer": "cus_foreign",
            "status": "active",
            "items": {"data": [{"price": {"id": "basic"}}]},
        },
        None,
    )
    with (
        patch.object(stripe.Subscription, "retrieve", return_value=current),
        pytest.raises(ValueError, match="not linked"),
    ):
        billing.webhook(payload, signed(payload, billing.secret))
