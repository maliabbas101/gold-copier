"""Exness API adapter for the existing durable worker/risk/target engine.

Only event-correlated copier IDs are managed. A confirmed ACK without an entity
correlation remains uncertain instead of adopting another trader's position.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import hashlib
import time
from types import SimpleNamespace as NS
from urllib.parse import urlencode

from .broker_v2 import PlatformBroker, MAGIC
from .exness_api import ExnessClient, ExnessStreams, ExnessError


def decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite API number")
    return format(number, "f")


class ExnessBroker(PlatformBroker):
    def __init__(self, cfg, ledger, client=None, streams=None):
        self.cfg = cfg
        self.baseline = cfg.setdefault("baseline", {})
        credentials = cfg.pop("api_credentials")
        client = client or ExnessClient(
            cfg["login"],
            credentials["api_key"],
            credentials["private_key"],
            credentials["gateway"],
        )
        client.discover()
        self.mt = ExnessFacade(client, streams or ExnessStreams(client), ledger, self)
        self.check_account()

    def operation_keys(self, requests, operation):
        for index, request in enumerate(requests):
            raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            request["_idempotency"] = (
                f"gc-{operation}-{index}-" + hashlib.sha256(raw).hexdigest()[:12]
            )

    def check_account(self):
        a = self.mt.account_info()
        if str(a.login) != self.cfg["login"]:
            raise ValueError("Exness API returned the wrong account")
        return a

    def prepare_entry(self, signal, risk, job_id):
        if not self.cfg.get("allow_live"):
            raise ValueError(
                "Direct Exness execution is disabled; connection is read-only"
            )
        if not self.mt.streams.ready:
            raise ValueError("Exness event stream is not ready")
        a = self.check_account()
        if not a.trade_allowed:
            raise ValueError("Exness account trading is disabled or close-only")
        symbol = self.entry_symbol(signal)
        if any(
            p.symbol == symbol and p.magic != MAGIC for p in self.mt.positions_get()
        ):
            raise ValueError(
                "External exposure exists on this instrument; use an isolated API account"
            )
        self.mt.command_key = "entry-" + job_id
        return super().prepare_entry(signal, risk, job_id)

    def prepare_control(self, command):
        if not self.cfg.get("allow_live"):
            raise ValueError("Direct Exness execution is disabled")
        return super().prepare_control(command)

    def submit(self, request):
        result = self.mt.order_send(request)
        if result.retcode == -1:
            return {
                "state": "rejected",
                "detail": "Exness rejected the operation; inspect broker history and trading limits",
            }
        if result.retcode != self.mt.TRADE_RETCODE_DONE:
            return {
                "state": "uncertain",
                "detail": "Exness outcome or entity correlation is unconfirmed; reconcile operation history",
            }
        return {
            "state": "executed",
            "detail": "Exness confirmed operation",
            "ticket": int(result.order),
        }

    def preflight(self, request):
        if request.get("symbol"):
            c = self.mt.client.conditions(request["symbol"])
            if c.get("trade_mode") != "enabled":
                raise ValueError("Exness instrument trading is disabled")
            for field in ("price", "sl", "tp"):
                if (
                    request.get(field)
                    and Decimal(str(request[field])).normalize().as_tuple().exponent
                    < -c["point_digits"]
                ):
                    raise ValueError("Price precision exceeds Exness instrument digits")
        # Final margin/session/HMR validation is performed by the Exness operation.
        if request.get("volume", 0) <= 0 and request.get("action") in (1, 5):
            raise ValueError("Invalid volume")

    def snapshot_price(self, position):
        streams = self.mt.streams
        if not hasattr(streams, "ticks"):
            return None
        with streams.lock:
            streams.symbols.add(position.symbol)
            tick = streams.ticks.get(position.symbol)
        if not streams.ready or not tick or abs(time.time() - tick["time"]) > 10:
            return None
        return tick["bid"] if position.type == 0 else tick["ask"]

    def snapshot(self):
        a = self.check_account()
        positions = self.mt.positions_get()
        orders = self.mt.orders_get()
        now = datetime.now(timezone.utc)
        if (
            time.time() - getattr(self, "_snapshot_history_at", 0) >= 5
            or getattr(self, "_snapshot_day", None) != now.date()
        ):
            self._snapshot_deals = self.mt.history_deals_get(
                now.replace(hour=0, minute=0, second=0, microsecond=0), now
            )
            self._snapshot_history_at = time.time()
            self._snapshot_day = now.date()
        deals = self._snapshot_deals
        return {
            "login": str(a.login),
            "server": self.cfg["server"],
            "mode": "live",
            "balance": a.balance,
            "equity": a.equity,
            "currency": a.currency,
            "instruments": self.mt.client.instruments()[:1000],
            "daily_pnl": sum(d.profit + d.commission + d.swap + d.fee for d in deals),
            "positions": [
                {
                    "ticket": str(p.ticket),
                    "symbol": p.symbol,
                    "side": "BUY" if p.type == 0 else "SELL",
                    "volume": p.volume,
                    "price_open": p.price_open,
                    "price_current": self.snapshot_price(p),
                    "sl": p.sl,
                    "tp": p.tp,
                    "profit": None,
                    "copier": p.magic == MAGIC,
                }
                for p in positions
            ],
            "orders": [
                {
                    "ticket": str(o.ticket),
                    "symbol": o.symbol,
                    "type": str(o.type),
                    "volume": o.volume_current,
                    "price_open": o.price_open,
                    "sl": o.sl,
                    "tp": o.tp,
                    "copier": o.magic == MAGIC,
                }
                for o in orders
            ],
            "deals": [],
        }


class ExnessFacade:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_MODIFY = 7
    TRADE_ACTION_REMOVE = 8
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 2
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1

    def __init__(self, client, streams, ledger, broker):
        self.client, self.streams, self.ledger, self.broker = (
            client,
            streams,
            ledger,
            broker,
        )
        row = ledger.db.execute(
            "SELECT value FROM metadata WHERE key='exness_bindings'"
        ).fetchone()
        self.bindings = json.loads(row[0]) if row else {"positions": {}, "orders": {}}
        self.cached = None
        self.cached_at = 0
        self.command_key = ""

    def persist(self):
        self.ledger.db.execute(
            "INSERT INTO metadata VALUES('exness_bindings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(self.bindings),),
        )
        self.ledger.db.commit()

    def raw(self):
        if self.cached is None or time.time() - self.cached_at > 1:
            self.cached = self.client.snapshot()
            self.cached_at = time.time()
        # A pending fill can arrive without the original client request ID. Correlate its tracked order ID.
        with self.streams.lock:
            events = list(self.streams.events.values())
        changed = False
        for event in events:
            for order in event.get("payload", {}).get("orders", []):
                tag = self.bindings["orders"].get(str(order.get("order_id")))
                if (
                    tag
                    and order.get("position_id")
                    and str(order["position_id"]) not in self.bindings["positions"]
                ):
                    self.bindings["positions"][str(order["position_id"])] = tag
                    changed = True
        if changed:
            self.persist()
        return self.cached

    def account_info(self):
        details = self.client.account()
        state = self.raw()["account_state"]
        settings = details["settings"]
        return NS(
            login=int(details["account_id"]),
            server=self.broker.cfg["server"],
            balance=float(state["balance"]),
            equity=float(state["equity"]),
            currency=settings["currency"],
            trade_allowed=settings["trade_mode"] == "enabled"
            and settings["account_status"] == "active",
        )

    def symbol_select(self, symbol, visible):
        return symbol in self.client.instruments()

    def symbol_info(self, symbol):
        c = self.client.conditions(symbol)
        return NS(
            volume_min=float(c["volume_min"]),
            volume_max=float(c["volume_max"]),
            volume_step=float(c["volume_step"]),
            point=10 ** -c["point_digits"],
            digits=c["point_digits"],
            trade_stops_level=0,
        )

    def symbol_info_tick(self, symbol):
        return NS(**self.streams.quote(symbol))

    def order_calc_profit(self, kind, symbol, volume, open_price, close_price):
        c = self.client.conditions(symbol)
        a = self.account_info()
        if c.get("calc_mode") not in (
            "forex",
            "forex_no_leverage",
            "cfd",
            "cfd_leverage",
        ):
            raise ValueError("Unsupported Exness calculation mode")
        result = (
            (Decimal(str(close_price)) - Decimal(str(open_price)))
            * Decimal(str(c["contract_size"]))
            * Decimal(str(volume))
            * (1 if kind == 0 else -1)
        )
        currency = c.get("quote_currency")
        if not currency:
            raise ValueError("Instrument quote currency is unavailable")
        if currency != a.currency:
            instruments = self.client.instruments()
            direct = currency + a.currency
            inverse = a.currency + currency
            if direct in instruments:
                q = self.streams.quote(direct)
                result *= Decimal(str(q["ask"] if result < 0 else q["bid"]))
            elif inverse in instruments:
                q = self.streams.quote(inverse)
                result /= Decimal(str(q["bid"] if result < 0 else q["ask"]))
            else:
                raise ValueError(
                    "No explicit Exness conversion instrument for account-currency risk"
                )
        return float(result)

    def positions_get(self, **kwargs):
        raw = self.raw()
        orders = raw.get("orders", [])
        out = []
        for p in raw.get("positions", []):
            if (
                p["state"] != "open"
                or kwargs.get("symbol", p["instrument"]) != p["instrument"]
            ):
                continue
            sltp = next(
                (
                    o
                    for o in orders
                    if str(o.get("position_id")) == str(p["position_id"])
                    and o["order_type"] == "sltp"
                    and o["state"] == "placed"
                ),
                {},
            )
            tag = self.bindings["positions"].get(str(p["position_id"]))
            out.append(
                NS(
                    ticket=int(p["position_id"]),
                    identifier=int(p["position_id"]),
                    symbol=p["instrument"],
                    type=0 if p["direction"] == "buy" else 1,
                    volume=float(p["volume"]),
                    price_open=float(p["open_price"]),
                    sl=float(sltp.get("stop_loss_price") or 0),
                    tp=float(sltp.get("take_profit_price") or 0),
                    magic=MAGIC if tag else 0,
                    comment=tag or "external",
                )
            )
        return tuple(out)

    def orders_get(self, **kwargs):
        out = []
        for o in self.raw().get("orders", []):
            if (
                o["state"] != "placed"
                or o["order_type"] not in ("limit", "stop")
                or kwargs.get("symbol", o.get("instrument")) != o.get("instrument")
            ):
                continue
            tag = self.bindings["orders"].get(str(o["order_id"]))
            kind = (2 if o["order_type"] == "limit" else 4) + (
                0 if o["direction"] == "buy" else 1
            )
            out.append(
                NS(
                    ticket=int(o["order_id"]),
                    symbol=o["instrument"],
                    type=kind,
                    volume_current=float(o["volume"]),
                    price_open=float(o["price"]),
                    sl=float(o.get("stop_loss_price") or 0),
                    tp=float(o.get("take_profit_price") or 0),
                    magic=MAGIC if tag else 0,
                    comment=tag or "external",
                    type_time=0,
                    time_expiration=0,
                )
            )
        return tuple(out)

    def history_orders_get(self, *args):
        return ()  # Never infer ownership from unrelated API history.

    def history_deals_get(self, start, end):
        path = f"/v1/history/accounts/{self.client.account_id}/deals?" + urlencode(
            {"from": start.isoformat(), "to": end.isoformat(), "limit": 100}
        )
        data = self.client.request("GET", path)
        # Pagination is mandatory for an accurate day baseline; fail closed on a truncated day.
        if data.get("next_cursor") or data.get("has_more"):
            raise ValueError(
                "Exness day history exceeds one page; reconciliation is required"
            )

        def amount(value):
            return (
                float(value.get("amount", 0))
                if isinstance(value, dict)
                else float(value or 0)
            )

        return tuple(
            NS(
                type=0 if d.get("direction") == "buy" else 1,
                profit=amount(d.get("profit")),
                commission=amount(d.get("commission")),
                swap=amount(d.get("swap")),
                fee=amount(d.get("fee")),
            )
            for item in data.get("items", [])
            for d in [item["deal"]]
            if d.get("type") != "balance"
        )

    def order_send(self, r):
        key = r.get("_idempotency") or self.command_key
        # The durable worker assigns a unique key to every stage before submission.
        if not key:
            raise ValueError("Missing durable operation key")
        body = None
        method = "POST"
        action = r["action"]
        resource = "positions"
        is_entry = action in (1, 5) and "position" not in r
        if is_entry:
            body = {
                "instrument": r["symbol"],
                "side": "buy" if r["type"] in (0, 2, 4) else "sell",
                "volume": decimal(r["volume"]),
                "stop_loss_price": decimal(r["sl"]),
                "comment": r["comment"],
            }
            if r.get("tp"):
                body["take_profit_price"] = decimal(r["tp"])
            if action == 5:
                resource = "orders"
                body.update(
                    type="limit" if r["type"] in (2, 3) else "stop",
                    price=decimal(r["price"]),
                )
            elif self.client.conditions(r["symbol"]).get("exec_mode") == "instant":
                body.update(
                    price=decimal(r["price"]), deviation=decimal(r["deviation"])
                )
        elif action == 1:
            resource = (
                "positions/"
                + str(r["position"])
                + "?"
                + urlencode({"volume": decimal(r["volume"])})
            )
            method = "DELETE"
        elif action == 8:
            resource = "orders/" + str(r["order"])
            method = "DELETE"
        elif action == 7:
            resource = "orders/" + str(r["order"])
            method = "PATCH"
            body = {
                "price": decimal(r["price"]),
                "stop_loss_price": decimal(r["sl"]),
                "take_profit_price": decimal(r["tp"]) if r.get("tp") else None,
            }
        elif action == 6:
            sltp = next(
                (
                    o
                    for o in self.raw().get("orders", [])
                    if o["order_type"] == "sltp"
                    and str(o.get("position_id")) == str(r["position"])
                    and o["state"] == "placed"
                ),
                None,
            )
            if sltp:
                resource = "orders/" + str(sltp["order_id"])
                method = "PATCH"
                body = {
                    "stop_loss_price": decimal(r["sl"]),
                    "take_profit_price": decimal(r["tp"]) if r.get("tp") else None,
                }
            else:
                resource = "orders"
                body = {
                    "type": "sltp",
                    "position_id": str(r["position"]),
                    "stop_loss_price": decimal(r["sl"]),
                    **({"take_profit_price": decimal(r["tp"])} if r.get("tp") else {}),
                }
        else:
            raise ValueError("Unsupported Exness instruction")

        def ack(value):
            self.ledger.db.execute(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("ack:" + key, json.dumps(value)),
            )
            self.ledger.db.commit()

        outcome = self.client.mutate(method, resource, body, key, ack, self.streams)
        self.cached_at = 0
        if outcome["status"] == "rejected":
            return NS(retcode=-1, order=0)
        if outcome["status"] not in ("success", "confirmed"):
            return NS(retcode=-2, order=0)
        entities = (outcome.get("event") or {}).get("payload", {})
        ticket = r.get("position") or r.get("order") or 0
        if is_entry:
            matches = (
                [
                    p
                    for p in entities.get("positions", [])
                    if p.get("instrument") == r["symbol"] and p.get("state") == "open"
                ]
                if action == 1
                else [
                    o
                    for o in entities.get("orders", [])
                    if o.get("instrument") == r["symbol"]
                    and o.get("order_type") in ("limit", "stop")
                ]
            )
            if len(matches) != 1:
                return NS(retcode=-3, order=0)
            entity = matches[0]
            ticket = int(entity["position_id" if action == 1 else "order_id"])
            if action == 1 and str(ticket) in self.bindings["positions"]:
                return NS(retcode=-3, order=0)
            self.bindings["positions" if action == 1 else "orders"][str(ticket)] = r[
                "comment"
            ]
            self.persist()
        return NS(retcode=self.TRADE_RETCODE_DONE, order=int(ticket))

    def shutdown(self):
        self.streams.close()
        self.client.close()
