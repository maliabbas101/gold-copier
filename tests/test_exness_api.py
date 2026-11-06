import base64
import hashlib
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_platform import workspace
from goldcopier.exness_api import ExnessClient, ExnessError, host, signing_key


def keys():
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ) + key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return key, raw.hex()


def decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_signatures_cover_exact_body_query_and_idempotency():
    private, raw = keys()
    c = ExnessClient(
        "123",
        "public-key",
        raw,
        "https://gateway.exness.com",
        httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    path = "/v1/trading/accounts/123/positions/7?volume=0.01&price=1.08001"
    body = b'{"instrument":"EURUSD","volume":"0.10"}'
    headers = c.headers("POST", path, body, "entry-stable-key", 123456)
    payload = decode(headers["EXN-DATA"])
    private.public_key().verify(decode(headers["EXN-SIGN"]), payload)
    data = json.loads(payload)
    assert data["path"] == path and data["idempotency_key"] == "entry-stable-key"
    assert data["body_hash"] == base64.urlsafe_b64encode(
        hashlib.sha256(body).digest()
    ).decode().rstrip("=")
    assert "=" not in headers["EXN-DATA"] and "=" not in headers["EXN-SIGN"]
    assert c.headers("GET", path)["EXN-IDEMPOTENCY-KEY"] == ""
    with pytest.raises(ValueError):
        c.headers("POST", path)


def test_discovery_instruments_and_async_confirmation():
    _, raw = keys()
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/trading/access-point":
            return httpx.Response(
                200, json={"account_id": "123", "access_point": "https://ap.exness.com"}
            )
        if request.url.path.endswith("/instruments"):
            return httpx.Response(200, json={"instruments": ["EURUSD", "XAUUSD"]})
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "operation_id": "789",
                    "client_request_id": "my-order",
                },
            )
        return httpx.Response(200, json={"operation_id": "789", "status": "confirmed"})

    c = ExnessClient(
        "123",
        "api-key",
        raw,
        "https://gateway.exness.com",
        httpx.MockTransport(handler),
    )
    assert c.instruments() == ["EURUSD", "XAUUSD"]
    assert (
        seen[0].url.host == "gateway.exness.com" and seen[1].url.host == "ap.exness.com"
    )
    acknowledgements = []
    outcome = c.mutate(
        "POST",
        "positions",
        {"instrument": "EURUSD", "side": "buy", "volume": "0.01"},
        "my-order",
        acknowledgements.append,
    )
    assert (
        outcome["status"] == "confirmed"
        and acknowledgements[0]["operation_id"] == "789"
    )
    assert len([r for r in seen if r.method == "POST"]) == 1


def test_ambiguous_submission_is_never_retried():
    _, raw = keys()
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/trading/access-point":
            return httpx.Response(
                200, json={"account_id": "123", "access_point": "https://ap.exness.com"}
            )
        raise httpx.ReadTimeout("uncertain")

    c = ExnessClient(
        "123", "key", raw, "https://gateway.exness.com", httpx.MockTransport(handler)
    )
    with pytest.raises(httpx.ReadTimeout):
        c.mutate("POST", "positions", {}, "no-replay")
    assert len([r for r in calls if r.method == "POST"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.exness.com",
        "https://evil.example",
        "https://exness.com.evil.example",
        "https://api.exness.com/private",
        "https://user@api.exness.com",
        "https://127.0.0.1",
    ],
)
def test_untrusted_hosts_never_receive_credentials(url):
    with pytest.raises(ValueError):
        host(url)


def test_no_signed_redirect_following_and_rate_limit_backoff():
    _, raw = keys()
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    c = ExnessClient(
        "123", "key", raw, "https://gateway.exness.com", httpx.MockTransport(handler)
    )
    with pytest.raises(ExnessError):
        c.discover()
    assert len(calls) == 1
    c.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                429, headers={"Retry-After": "60"}, json={"code": 3015}
            )
        )
    )
    with pytest.raises(ExnessError):
        c.discover()
    assert c.cool_until > time.monotonic() + 50


def test_api_credentials_encrypted_and_worker_scoped(workspace):
    _, key = keys()
    w = workspace
    body = {
        "name": "Exness direct",
        "account_id": "98765",
        "api_key": "exnsk-test-public",
        "private_key": key,
        "gateway": "https://gateway.exness.com",
        "consent": True,
    }
    response = w.client.post("/api/accounts/exness", json=body)
    assert response.status_code == 200, response.text
    account = response.json()["id"]
    assert response.json()["read_only"]
    d = w.client.get("/api/dashboard").json()
    assert key not in json.dumps(d) and "exnsk-test-public" not in json.dumps(d)
    token = w.client.post(f"/api/accounts/{account}/token").json()["token"]
    config = w.client.get(
        "/api/worker/config", headers={"Authorization": "Bearer " + token}
    ).json()
    assert (
        config["connector"] == "exness"
        and config["api_credentials"]["private_key"] == key
    )
    assert not config["allow_live"] and config["password"] is None
    assert (
        w.client.post(
            "/api/worker/claim", headers={"Authorization": "Bearer " + token}
        ).json()
        is None
    )
