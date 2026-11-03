"""Read-only broker observer with durable, idempotent direct signal delivery.

Baseline existing positions; only new, protected fills are copied. The source
never submits broker orders. Missing positions require broker close evidence.
"""

import hashlib
import json
import time

from .broker_v2 import MAGIC


class MasterObserver:
    def __init__(self, broker, ledger, client):
        self.broker, self.ledger, self.client = broker, ledger, client
        self.last_heartbeat = 0
        self.status = "Trader watcher ready; waiting for a new protected fill"
        self.last_status = None
        self.first_poll = True

    def read(self):
        self.broker.check_account()
        positions = self.broker.mt.positions_get()
        if positions is None:
            raise ValueError("Trader positions are unavailable; no close is inferred")
        result = {}
        for p in positions:
            if getattr(p, "magic", 0) == MAGIC:
                continue
            ident = str(getattr(p, "identifier", p.ticket))
            result[ident] = {
                "ticket": str(p.ticket),
                "symbol": p.symbol,
                "side": "BUY" if p.type == 0 else "SELL",
                "entry": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "volume": p.volume,
                "opened": getattr(p, "time", None),
            }
        return result

    def close_confirmed(self, ident):
        if self.broker.cfg.get("connector") == "exness":
            streams = self.broker.mt.streams
            with streams.lock:
                events = list(streams.events.values())
            return any(
                str(p.get("position_id")) == ident and p.get("state") == "closed"
                for event in events
                for p in event.get("payload", {}).get("positions", [])
            ) or any(
                str(p.get("position_id")) == ident and p.get("state") == "closed"
                for p in self.broker.mt.raw().get("positions", [])
            )
        deals = self.broker.mt.history_deals_get(position=int(ident))
        if deals is None:
            return False
        incoming = sum(
            d.volume
            for d in deals
            if d.entry == getattr(self.broker.mt, "DEAL_ENTRY_IN", 0)
        )
        outgoing = sum(
            d.volume
            for d in deals
            if d.entry
            in (
                getattr(self.broker.mt, "DEAL_ENTRY_OUT", 1),
                getattr(self.broker.mt, "DEAL_ENTRY_OUT_BY", 3),
            )
        )
        return incoming > 0 and outgoing >= incoming - 1e-8

    def load(self, key):
        row = self.ledger.db.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, state):
        self.ledger.db.execute(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(state, separators=(",", ":"), allow_nan=False)),
        )
        self.ledger.db.commit()

    def flush(self, key, state, source):
        plan = state.get("plan")
        if not plan:
            return
        for event in plan["events"]:
            if event.get("done"):
                continue
            body = event["body"]
            if time.time() - body["created"] > 60:
                receipt = self.client.get(
                    "/api/worker/master-events/" + body["source_id"]
                )
                receipt.raise_for_status()
                accepted = receipt.json()["processed"]
                if not accepted:
                    self.status = (
                        "Trader event expired; no automatic replay: "
                        + body["source_id"]
                    )
            else:
                response = self.client.post("/api/worker/master-events", json=body)
                if response.status_code == 400:
                    receipt = self.client.get(
                        "/api/worker/master-events/" + body["source_id"]
                    )
                    receipt.raise_for_status()
                    accepted = receipt.json()["processed"]
                    if not accepted:
                        self.status = (
                            "Trader event rejected: "
                            + str(response.json().get("detail", "invalid signal"))[:180]
                        )
                else:
                    response.raise_for_status()
                    accepted = True
                    result = response.json()
                    reasons = "; ".join(
                        dict.fromkeys(s["reason"] for s in result.get("skipped", []))
                    )
                    self.status = (
                        f"{result.get('queued', 0)} follower command(s) queued; Discord waits for execution. "
                        + (
                            reasons
                            or (
                                ""
                                if result.get("queued")
                                else "No eligible followers. See Activity."
                            )
                        )
                    )[:300]
            if event["kind"] == "entry" and not accepted:
                plan["positions"][event["position"]]["mirrored"] = False
                plan["positions"][event["position"]]["blocked"] = True
                plan["positions"][event["position"]]["reason"] = self.status
            event["done"] = True
            self.save(key, state)
        # Keep positions observed while the source was paused baselined.
        for ident, record in state["positions"].items():
            if ident not in plan["positions"] and record.get("baseline"):
                plan["positions"][ident] = record
        state["positions"] = plan["positions"]
        state.pop("plan", None)
        self.save(key, state)

    def step(self, source, pairs):
        current = self.read()
        key = "master-source:" + source["id"]
        state = self.load(key)
        now = time.time()
        if state is None:
            state = {
                "positions": {
                    i: {"data": p, "baseline": True, "mirrored": False}
                    for i, p in current.items()
                },
                "sequence": 0,
            }
            self.save(key, state)
            self.first_poll = False
            self.status = f"Trader watcher ready; {len(current)} existing position(s) baselined, not copied"
            return
        if self.first_poll:
            planned = state.get("plan", {}).get("positions", {})
            for ident, p in current.items():
                if ident not in state["positions"] and ident not in planned:
                    state["positions"][ident] = {
                        "data": p,
                        "baseline": True,
                        "mirrored": False,
                    }
            self.first_poll = False
            self.save(key, state)
        if not source["enabled"] or not source["active"]:
            for ident, p in current.items():
                if ident not in state["positions"]:
                    state["positions"][ident] = {
                        "data": p,
                        "baseline": True,
                        "mirrored": False,
                    }
            self.save(key, state)
            self.status = "Trader source is paused; new positions are not backfilled"
            return
        self.status = "Trader watcher ready; waiting for a new protected fill"
        self.flush(key, state, source)
        previous = state["positions"]
        target = {}
        events = []
        mapping = {
            p["broker_symbol"]: p["pair"] for p in pairs if p.get("enabled", True)
        }

        def event(kind, ident, text):
            state["sequence"] += 1
            source_id = (
                "m-"
                + hashlib.sha256(
                    (
                        source["id"]
                        + ":"
                        + ident
                        + ":"
                        + str(state["sequence"])
                        + ":"
                        + kind
                    ).encode()
                ).hexdigest()[:40]
            )
            events.append(
                {
                    "position": ident,
                    "kind": kind,
                    "body": {
                        "analyst_id": source["analyst_id"],
                        "source_id": source_id,
                        "text": text,
                        "created": now,
                        "master_entry": (
                            current.get(ident)
                            or previous.get(ident, {}).get("data", {})
                        ).get("entry"),
                    },
                }
            )
            return source_id

        for ident, p in current.items():
            before = previous.get(ident)
            if before and before.get("baseline"):
                target[ident] = {**before, "data": p}
                continue
            record = (
                dict(before)
                if before
                else {"data": p, "first_seen": now, "mirrored": False}
            )
            target[ident] = record
            if record.get("blocked"):
                self.status = (
                    record.get("reason")
                    or "A trader position needs operator review; see Activity"
                )
                record["data"] = p
                continue
            if not record["mirrored"]:
                if now - record["first_seen"] > 60 or (
                    p.get("opened") and now - p["opened"] > 60
                ):
                    record["blocked"] = True
                    self.status = "New trader position expired before it had a mapped symbol and SL"
                    record["reason"] = self.status
                    continue
                symbol = mapping.get(p["symbol"])
                if not symbol or p["sl"] <= 0:
                    self.status = f"Waiting for {'pair mapping' if not symbol else 'stop loss'} on trader position {ident} (maximum 60 seconds)"
                    continue
                text = f"{symbol} {p['side']} CMP\nSL: {p['sl']}" + (
                    f"\nTP: {p['tp']}" if p["tp"] else ""
                )
                reference = event("entry", ident, text)
                record.update(mirrored=True, reference=reference, data=p)
                self.status = "Trader fill observed; waiting for follower execution before Discord output"
                continue
            old = record["data"]
            reference = record["reference"]
            if p["side"] != old["side"] or p["volume"] > old["volume"] + 1e-8:
                self.status = "Trader scale-in/reversal needs separate hedging positions; it was not copied"
                record["blocked"] = True
                record["reason"] = self.status
                continue
            if p["sl"] != old["sl"] or p["tp"] != old["tp"]:
                if p["sl"] <= 0 or (old["tp"] and not p["tp"]):
                    self.status = "Removing trader SL/TP is not mirrored; follower protection is preserved"
                else:
                    point = getattr(self.broker.mt.symbol_info(p["symbol"]), "point", 0)
                    if abs(p["sl"] - p["entry"]) <= point:
                        event("be", ident, f"BE SIGNAL: {reference}")
                        if p["tp"] and p["tp"] != old["tp"]:
                            event(
                                "tp",
                                ident,
                                f"UPDATE SIGNAL: {reference}\nTP: {p['tp']}",
                            )
                    else:
                        event(
                            "modify",
                            ident,
                            f"UPDATE SIGNAL: {reference}\nSL: {p['sl']}"
                            + (f"\nTP: {p['tp']}" if p["tp"] else ""),
                        )
            if p["volume"] < old["volume"] - 1e-8:
                percent = (old["volume"] - p["volume"]) / old["volume"] * 100
                event(
                    "partial",
                    ident,
                    f"PARTIAL SIGNAL: {reference}\nPERCENT: {percent:.8f}",
                )
            record["data"] = p
        for ident, record in previous.items():
            if ident in current:
                continue
            if record.get("mirrored"):
                if not self.close_confirmed(ident):
                    target[ident] = record
                    self.status = "Waiting for broker-confirmed close history; disappearance alone is not a close command"
                    continue
                event("close", ident, "CLOSE SIGNAL: " + record["reference"])
        if events:
            state["plan"] = {"events": events, "positions": target}
            self.save(key, state)  # Persist IDs and payloads before any HTTP request.
            self.flush(key, state, source)
        else:
            state["positions"] = target
            self.save(key, state)
