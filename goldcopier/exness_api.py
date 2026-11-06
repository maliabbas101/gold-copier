"""Official Exness Trader API transport (OpenAPI 1.0.2).

Ed25519 signatures, account Access Point discovery, explicit idempotency keys,
and asynchronous confirmation. No order is retried on ambiguous HTTP outcomes.
"""

import asyncio
import base64
import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit, urlencode

import aiohttp
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def signing_key(text):
    text = text.strip()
    if text.startswith("-----BEGIN PRIVATE KEY-----"):
        key = serialization.load_pem_private_key(text.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("An Ed25519 private key is required")
        return key
    if not re.fullmatch(r"[a-fA-F0-9]{128}", text):
        raise ValueError("Use the Exness 128-hex private key or Ed25519 PKCS#8 PEM")
    raw = bytes.fromhex(text)
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if public != raw[32:]:
        raise ValueError("Private key public half does not match its seed")
    return key


def host(value):
    u = urlsplit(value)
    if (
        u.scheme != "https"
        or u.username
        or u.password
        or u.port not in (None, 443)
        or u.path not in ("", "/")
        or u.query
        or u.fragment
    ):
        raise ValueError(
            "Use the HTTPS Gateway URL shown by Exness, without path, credentials, query or fragment"
        )
    if not u.hostname or not any(
        u.hostname == d or u.hostname.endswith("." + d)
        for d in ("exness.com", "exness-api.com")
    ):
        raise ValueError("API host must belong to exness.com or exness-api.com")
    return "https://" + u.hostname


class ExnessError(ValueError):
    def __init__(self, status, code=None):
        self.status, self.code = status, code
        super().__init__(
            f"Exness API rejected request (HTTP {status}, code {code or 'unknown'})"
        )


class ExnessClient:
    def __init__(self, account_id, api_key, private_key, gateway, transport=None):
        if (
            not re.fullmatch(r"[1-9][0-9]{0,19}", str(account_id))
            or int(account_id) > 2**64 - 1
        ):
            raise ValueError("Invalid Exness account ID")
        self.account_id = str(account_id)
        self.api_key = api_key
        self.key = signing_key(private_key)
        self.gateway = host(gateway)
        self.access_point = None
        self.http = httpx.Client(
            timeout=15, follow_redirects=False, transport=transport, trust_env=False
        )
        self.condition_cache = {}
        self.read_cache = {}
        self.cool_until = 0

    def headers(self, method, path, body=b"", idempotency="", timestamp=None):
        if method != "GET" and not re.fullmatch(r"[A-Za-z0-9._~-]{5,128}", idempotency):
            raise ValueError("Mutations require a stable idempotency key")
        if method == "GET" and idempotency:
            raise ValueError("GET idempotency key must be empty")
        stamp = timestamp if timestamp is not None else int(time.time() * 1000)
        payload = json.dumps(
            {
                "api_key": self.api_key,
                "idempotency_key": idempotency,
                "timestamp": stamp,
                "sign_version": 1,
                "method": method,
                "path": path,
                "body_hash": b64(hashlib.sha256(body).digest()),
            },
            separators=(",", ":"),
        ).encode()
        return {
            "EXN-API-KEY": self.api_key,
            "EXN-IDEMPOTENCY-KEY": idempotency,
            "EXN-TIMESTAMP": str(stamp),
            "EXN-SIGN-VERSION": "1",
            "EXN-DATA": b64(payload),
            "EXN-SIGN": b64(self.key.sign(payload)),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def request(self, method, path, body=None, idempotency="", gateway=False):
        if time.monotonic() < self.cool_until:
            raise ExnessError(429, 3015)
        if not path.startswith("/v1/") or "#" in path:
            raise ValueError("Invalid API path")
        if not gateway and not self.access_point:
            self.discover()
        raw = (
            b""
            if body is None
            else json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        )
        endpoint = self.gateway if gateway else self.access_point
        response = self.http.request(
            method,
            endpoint + path,
            content=raw,
            headers=self.headers(method, path, raw, idempotency),
        )
        if response.status_code == 429:
            try:
                delay = max(1, float(response.headers.get("Retry-After", "60")))
            except ValueError:
                delay = 60
            self.cool_until = time.monotonic() + delay
        if response.status_code not in (200, 201, 202, 204):
            try:
                code = response.json().get("code")
            except (ValueError, AttributeError):
                code = None
            raise ExnessError(response.status_code, code)
        return response.json() if response.content else {}

    def discover(self):
        result = self.request(
            "GET",
            "/v1/trading/access-point?account_id=" + self.account_id,
            gateway=True,
        )
        if result.get("account_id", self.account_id) != self.account_id:
            raise ValueError("Discovery returned the wrong account")
        self.access_point = host(result["access_point"])
        return self.access_point

    def configuration(self, resource):
        cached = self.read_cache.get(resource)
        ttl = 5 if resource == "account" else 60
        if not cached or time.time() - cached[0] > ttl:
            cached = (
                time.time(),
                self.request(
                    "GET", f"/v1/configuration/accounts/{self.account_id}/" + resource
                ),
            )
            self.read_cache[resource] = cached
        return cached[1]

    def account(self):
        return self.configuration("account")

    def instruments(self):
        return self.configuration("instruments")["instruments"]

    def limits(self):
        return self.configuration("limits")

    def conditions(self, symbol):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,11}", symbol):
            raise ValueError("Invalid Exness instrument name")
        cached = self.condition_cache.get(symbol)
        if not cached or time.time() - cached[0] > 60:
            cached = (
                time.time(),
                self.configuration("instruments/" + symbol + "/conditions"),
            )
            self.condition_cache[symbol] = cached
        return cached[1]

    def snapshot(self):
        return self.request("GET", f"/v1/trading/accounts/{self.account_id}/snapshot")

    def operation(self, operation_id):
        if not str(operation_id).isdigit():
            raise ValueError("Invalid operation ID")
        return self.request(
            "GET", f"/v1/trading/accounts/{self.account_id}/operations/{operation_id}"
        )

    def mutate(self, method, resource, body, key, on_ack=None, events=None, timeout=12):
        ack = self.request(
            method, f"/v1/trading/accounts/{self.account_id}/" + resource, body, key
        )
        if (
            ack.get("status") != "accepted"
            or not str(ack.get("operation_id", "")).isdigit()
        ):
            raise ValueError("Exness returned an invalid operation acknowledgement")
        if on_ack:
            on_ack(ack)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = events.result(key, ack["operation_id"]) if events else None
            if event is not None:
                return {
                    "operation_id": ack["operation_id"],
                    "status": event.get("status"),
                    "event": event,
                }
            state = self.operation(ack["operation_id"])
            if state["status"] in ("rejected", "failed"):
                return {"operation_id": ack["operation_id"], "status": state["status"]}
            if state["status"] == "confirmed":
                return {
                    "operation_id": ack["operation_id"],
                    "status": "confirmed",
                    "event": events.result(key, ack["operation_id"])
                    if events
                    else None,
                }
            time.sleep(0.5)
        return {"operation_id": ack["operation_id"], "status": "pending"}

    def close(self):
        self.http.close()


class ExnessStreams:
    """Two signed WebSockets. Disconnected/stale ticks cannot authorize entries."""

    def __init__(self, client):
        self.client = client
        self.ticks = {}
        self.events = {}
        self.symbols = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ready = False
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.run()), daemon=True
        )
        self.thread.start()

    def quote(self, symbol, timeout=5):
        with self.lock:
            self.symbols.add(symbol)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                tick = self.ticks.get(symbol)
            if tick and self.ready and abs(time.time() - tick["time"]) <= 10:
                return tick
            time.sleep(0.05)
        raise ValueError("No fresh Exness quote for " + symbol)

    def result(self, key, operation):
        with self.lock:
            return self.events.get(key) or self.events.get(str(operation))

    def consume(self, data):
        if data.get("event_type") == "transaction_event":
            with self.lock:
                for key in (data.get("client_request_id"), data.get("operation_id")):
                    if key:
                        self.events[str(key)] = data
                while len(self.events) > 1000:
                    self.events.pop(next(iter(self.events)))
        elif all(k in data for k in ("instrument", "bid", "ask", "timestamp")):
            timestamp = datetime.fromisoformat(
                data["timestamp"].replace("Z", "+00:00")
            ).timestamp()
            bid, ask = float(data["bid"]), float(data["ask"])
            if (
                not all(math.isfinite(v) for v in (bid, ask, timestamp))
                or not 0 < bid <= ask
            ):
                raise ValueError("Invalid Exness quote")
            with self.lock:
                self.ticks[data["instrument"]] = {
                    "bid": bid,
                    "ask": ask,
                    "time": timestamp,
                }

    async def channel(self, kind):
        path = f"/v1/server-events/accounts/{self.client.account_id}/ws/{kind}"
        trace = aiohttp.TraceConfig()

        async def no_redirect(*args):
            raise ValueError("Signed WebSocket redirects are forbidden")

        trace.on_request_redirect.append(no_redirect)
        async with aiohttp.ClientSession(
            trace_configs=[trace],
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
            trust_env=False,
        ) as session:
            async with session.ws_connect(
                self.client.access_point.replace("https://", "wss://") + path,
                headers=self.client.headers("GET", path),
                heartbeat=20,
                max_msg_size=1048576,
            ) as ws:
                subscribed = set()
                if kind == "events":
                    await ws.send_json(
                        {"id": "transactions", "subscribe": {"event": "transactions"}}
                    )
                    self.ready = True
                while not self.stop.is_set():
                    if kind == "ticks":
                        with self.lock:
                            wanted = set(self.symbols)
                        if wanted - subscribed:
                            await ws.send_json(
                                {
                                    "id": "ticks",
                                    "subscribe": {
                                        "event": "ticks",
                                        "instruments": sorted(wanted),
                                    },
                                }
                            )
                            subscribed = wanted
                    try:
                        message = await ws.receive(timeout=1)
                    except asyncio.TimeoutError:
                        continue
                    if message.type == aiohttp.WSMsgType.TEXT:
                        self.consume(json.loads(message.data))
                    elif message.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSE,
                    ):
                        raise ConnectionError("Exness stream closed")

    async def run(self):
        delay = 1
        while not self.stop.is_set():
            tasks = []
            try:
                tasks = [
                    asyncio.create_task(self.channel(k)) for k in ("events", "ticks")
                ]
                await asyncio.gather(*tasks)
            except Exception:
                self.ready = False
                with self.lock:
                    self.ticks.clear()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def close(self):
        self.stop.set()
        self.ready = False
