"""Durable Windows execution worker for the v2 platform."""

import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

import httpx

from .broker_v2 import PlatformBroker
from .platform_store import dump


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,job TEXT NOT NULL,request TEXT,result TEXT,ack INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS management(id TEXT PRIMARY KEY,state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        self.db.commit()

    def baseline(self):
        row = self.db.execute(
            "SELECT value FROM metadata WHERE key='baseline'"
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def save_baseline(self, value):
        self.db.execute(
            "INSERT INTO metadata VALUES('baseline',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (dump(value),),
        )
        self.db.commit()

    def reserve(self, job):
        self.db.execute(
            "INSERT INTO operations(id,job) VALUES(?,?)", (job["id"], dump(job))
        )
        self.db.commit()

    def prepared(self, ident, request):
        self.db.execute(
            "UPDATE operations SET request=? WHERE id=?", (dump(request), ident)
        )
        self.db.commit()

    def finish(self, ident, result):
        self.db.execute(
            "UPDATE operations SET result=?,ack=0 WHERE id=?", (dump(result), ident)
        )
        self.db.commit()

    def recover(self):
        for row in self.db.execute(
            "SELECT id,request FROM operations WHERE result IS NULL"
        ).fetchall():
            self.finish(
                row["id"],
                {
                    "state": "uncertain",
                    "detail": "Interrupted worker; review broker history before reconciliation",
                },
            )

    def unresolved(self):
        return bool(
            self.db.execute(
                "SELECT 1 FROM operations WHERE result IS NULL OR json_extract(result,'$.state')='uncertain' LIMIT 1"
            ).fetchone()
        )

    def management(self, ident):
        row = self.db.execute(
            "SELECT state FROM management WHERE id=?", (ident,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def save_management(self, ident, state):
        self.db.execute(
            "INSERT INTO management VALUES(?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
            (ident, dump(state)),
        )
        self.db.commit()

    def sync(self, client):
        self.pull_resolutions(client)
        for row in self.db.execute(
            "SELECT * FROM operations WHERE ack=0 AND result IS NOT NULL"
        ).fetchall():
            job = json.loads(row["job"])
            result = json.loads(row["result"])
            if row["id"].startswith("auto-"):
                response = client.post(
                    "/api/worker/auto-result",
                    json={
                        "id": row["id"],
                        "signal_id": job["signal"]["signal_id"],
                        "result": result,
                    },
                )
            else:
                response = client.post(
                    f"/api/worker/jobs/{row['id']}/result", json=result
                )
            response.raise_for_status()
            self.db.execute("UPDATE operations SET ack=1 WHERE id=?", (row["id"],))
            self.db.commit()

    def pull_resolutions(self, client):
        pending = [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM operations WHERE json_extract(result,'$.state')='uncertain'"
            )
        ]
        if not pending:
            return
        response = client.get(
            "/api/worker/reconciled", params={"ids": ",".join(pending[:100])}
        )
        response.raise_for_status()
        for server in response.json():
            row = self.db.execute(
                "SELECT result FROM operations WHERE id=?", (server["id"],)
            ).fetchone()
            if row and json.loads(row["result"] or "{}").get("state") == "uncertain":
                # Only an operator resolution (not an absent/queued job) can clear uncertainty.
                self.db.execute(
                    "UPDATE operations SET result=?,ack=1 WHERE id=?",
                    (server["result"], server["id"]),
                )
                self.db.commit()


def execute(job, broker, ledger):
    if ledger.db.execute(
        "SELECT 1 FROM operations WHERE id=?", (job["id"],)
    ).fetchone():
        raise ValueError("Duplicate operation blocked")
    ledger.reserve(job)
    signal = job["signal"]
    try:
        if time.time() >= job["expires"]:
            raise ValueError("Instruction expired")
        if signal["action"] == "entry":
            if (
                Path("PAUSE").exists()
                or ledger.unresolved()
                and ledger.db.execute(
                    "SELECT COUNT(*) FROM operations WHERE result IS NULL OR json_extract(result,'$.state')='uncertain'"
                ).fetchone()[0]
                > 1
            ):
                raise ValueError("Local worker is paused or needs reconciliation")
            requests = [broker.prepare_entry(signal, job["risk"], job["id"])]
            ledger.save_baseline(broker.baseline)
        else:
            command = dict(signal)
            if (
                command.get("origin") == "analyst"
                and command.get("signal_id")
                and job["risk"]["respect_manual_stops"]
            ):
                overrides = {}
                for original in ledger.db.execute(
                    "SELECT id FROM operations WHERE json_extract(job,'$.signal.signal_id')=? AND json_extract(job,'$.signal.action')='entry'",
                    (signal["signal_id"],),
                ):
                    for ticket, track in ledger.management(original["id"]).items():
                        overrides[ticket] = {
                            "sl": track.get("manual_sl", False),
                            "tp": track.get("manual_tp", False),
                        }
                command["overrides"] = overrides
            requests = broker.prepare_control(command)
        if time.time() >= job["expires"]:
            raise ValueError("Instruction expired during preflight")
    except Exception as exc:
        logging.warning("Preflight rejected: %s", exc)
        ledger.finish(job["id"], {"state": "rejected", "detail": str(exc)[:400]})
        return
    ledger.prepared(job["id"], requests)
    final = {"state": "executed", "detail": "Broker confirmed request"}
    try:
        for req in requests:
            final = broker.submit(req)
            if final["state"] != "executed":
                break
    except Exception:
        logging.exception("Broker submission interrupted")
        final = {
            "state": "uncertain",
            "detail": "Broker submission interrupted; reconcile order/deal history",
        }
    ledger.finish(job["id"], final)
    if final["state"] == "executed" and signal["action"] in ("modify", "breakeven"):
        for original in ledger.db.execute(
            "SELECT id,job FROM operations WHERE json_extract(job,'$.signal.action')='entry'"
        ).fetchall():
            managed = ledger.management(original["id"])
            entry = json.loads(original["job"])
            for request in requests:
                track = managed.get(str(request.get("position")))
                if not track:
                    continue
                if "sl" in request:
                    track["last_sl"] = request["sl"]
                    if signal.get("origin") == "member":
                        track["manual_sl"] = True
                if "tp" in request:
                    track["last_tp"] = request["tp"]
                    if signal.get("origin") == "member":
                        track["manual_tp"] = True
                    elif entry["signal"].get("targets") and request["tp"]:
                        entry["signal"]["targets"][-1] = request["tp"]
                        entry["signal"]["tp"] = request["tp"]
            ledger.save_management(original["id"], managed)
            ledger.db.execute(
                "UPDATE operations SET job=? WHERE id=?", (dump(entry), original["id"])
            )
            ledger.db.commit()


def monitor(broker, ledger, allow_partial=True):
    rows = ledger.db.execute(
        "SELECT * FROM operations WHERE json_extract(job,'$.signal.action')='entry' AND request IS NOT NULL AND json_extract(result,'$.state') IN ('executed','uncertain')"
    ).fetchall()
    for row in rows:
        job = json.loads(row["job"])
        signal = job["signal"]
        risk = job["risk"]
        state = ledger.management(row["id"])
        try:
            positions = broker.positions_for(signal["signal_id"])
            if not positions:
                continue  # May still be pending; never infer a failed entry from this.
            info, tick = broker.quote()
            for p in positions:
                key = str(p.ticket)
                track = state.setdefault(
                    key,
                    {
                        "initial": p.volume,
                        "last_sl": p.sl,
                        "last_tp": p.tp,
                        "manual_sl": False,
                        "manual_tp": False,
                        "stages": [],
                    },
                )
                if abs(p.sl - track["last_sl"]) > info.point:
                    track["manual_sl"] = True
                if abs(p.tp - track["last_tp"]) > info.point:
                    track["manual_tp"] = True
                buy = signal["side"] == "BUY"
                price = tick.bid if buy else tick.ask
                be_reached = price >= signal["be"] if buy else price <= signal["be"]
                tighter = p.sl >= p.price_open if buy else 0 < p.sl <= p.price_open
                if (
                    risk["be_enabled"]
                    and be_reached
                    and not tighter
                    and not (risk["respect_manual_stops"] and track["manual_sl"])
                ):
                    requests = broker.prepare_control(
                        {"action": "breakeven", "position": p.ticket}
                    )
                    for req in requests:
                        result = broker.submit(req)
                        if result["state"] == "executed":
                            track["last_sl"] = req["sl"]
                    ledger.save_management(row["id"], state)
                # Uncertain opens still receive BE protection, but never automatic partial exits.
                if (
                    not allow_partial
                    or ledger.unresolved()
                    or json.loads(row["result"])["state"] != "executed"
                    or not signal.get("multi_target")
                    or (risk["respect_manual_stops"] and track["manual_tp"])
                ):
                    continue
                for index, target in enumerate(signal["targets"]):
                    if (buy and price < target) or (not buy and price > target):
                        continue
                    ident = (
                        "auto-"
                        + hashlib.sha256(
                            f"{row['id']}:{p.ticket}:{index}".encode()
                        ).hexdigest()[:32]
                    )
                    if ledger.db.execute(
                        "SELECT 1 FROM operations WHERE id=?", (ident,)
                    ).fetchone():
                        continue
                    pct = [risk["tp1_close_pct"], risk["tp2_close_pct"]][index]
                    if pct <= 0:
                        continue
                    request = broker.close_request(
                        p, volume=track["initial"] * pct / 100
                    )
                    auto = {
                        "id": ident,
                        "signal": {
                            "action": "partial",
                            "signal_id": signal["signal_id"],
                        },
                        "risk": risk,
                        "expires": time.time() + 60,
                    }
                    ledger.reserve(auto)
                    if request is None:
                        ledger.finish(
                            ident,
                            {
                                "state": "rejected",
                                "detail": "Target allocation below broker minimum lot; no partial close",
                            },
                        )
                        continue
                    try:
                        broker.preflight(request)
                    except ValueError as exc:
                        ledger.finish(ident, {"state": "rejected", "detail": str(exc)})
                        continue
                    ledger.prepared(ident, [request])
                    try:
                        result = broker.submit(request)
                    except Exception:
                        result = {
                            "state": "uncertain",
                            "detail": "Automatic partial exit interrupted; reconcile broker history",
                        }
                    ledger.finish(ident, result)
                    # Refresh before another target; avoids stale volume and prevents repeated closes.
                    break
            ledger.save_management(row["id"], state)
        except Exception:
            logging.exception("Trade management inspection failed")


def terminal_options(cfg):
    options = {"path": cfg["terminal_path"]}
    password = cfg.pop("password", None)
    if cfg.get("hosting") == "self":
        password = os.environ.get("MT5_PASSWORD") or password
    if password:
        options.update(login=int(cfg["login"]), server=cfg["server"], password=password)
    return options


def main():
    from .runtime import find_terminal, load_env

    load_env(os.environ.get("COPIER_ENV_FILE") or ".env")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    url = (os.environ.get("COPIER_URL") or os.environ.get("PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("AGENT_TOKEN", "").strip()
    terminal_path = find_terminal()
    if not token:
        raise ValueError("Set AGENT_TOKEN in .env. Create one without the server using python -m goldcopier.manage worker-token, or use python -m goldcopier.manage run for automatic provisioning")
    if not terminal_path:
        raise ValueError("Set MT5_TERMINAL_PATH in .env to your terminal64.exe")
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    ):
        raise ValueError("HTTPS is required outside localhost")
    statepath = os.environ.get("WORKER_STATE") or "platform-worker.db"
    Path(statepath).parent.mkdir(parents=True, exist_ok=True)
    lock = Path(statepath + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    broker = None
    try:
        os.write(fd, str(os.getpid()).encode())
        ledger = Ledger(statepath)
        ledger.recover()
        with httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            timeout=10,
            follow_redirects=False,
        ) as client:
            response = client.get("/api/worker/config")
            response.raise_for_status()
            cfg = response.json()
            if cfg["demo_workspace"]:
                raise ValueError("Seeded demo workspaces cannot run broker workers")
            cfg.update(
                terminal_path=terminal_path,
                filling_mode=int(os.environ.get("MT5_FILLING_MODE") or "1"),
                allow_live=cfg["allow_live"]
                and os.environ.get("ALLOW_LIVE_TRADING", "false") == "true",
                baseline=ledger.baseline(),
            )
            import MetaTrader5 as mt

            init = terminal_options(cfg)
            if not mt.initialize(**init):
                raise ValueError(
                    f"MT5 terminal connection failed: {mt.last_error()}. "
                    "Check the MT5 login and server in the app and MT5_PASSWORD "
                    "in .env (or the terminal's saved login), then restart the server."
                )
            broker = PlatformBroker(cfg, mt)
            tick = 0
            while True:
                # Validate the token before software-managed exits. A revoked worker must terminate.
                try:
                    response = client.get("/api/worker/config")
                    if response.status_code in (401, 403):
                        logging.error(
                            "Worker authorization revoked; stopping terminal worker"
                        )
                        break
                    response.raise_for_status()
                    updated = response.json()
                    if updated["account_id"] != cfg["account_id"]:
                        raise ValueError("Worker account binding changed")
                    cfg["risk"] = updated["risk"]
                    cfg["enabled"] = updated["enabled"]
                    cfg["allow_live"] = (
                        updated["allow_live"]
                        and os.environ.get("ALLOW_LIVE_TRADING", "false") == "true"
                    )
                except httpx.HTTPError:
                    # BE is idempotent; partial closes require fresh authorization to reduce takeover races.
                    monitor(broker, ledger, allow_partial=False)
                    logging.exception(
                        "Control service unavailable; no entries or partial exits"
                    )
                    time.sleep(1)
                    continue
                monitor(broker, ledger)
                try:
                    if tick % 5 == 0:
                        snap = broker.snapshot()
                        try:
                            broker.protection(cfg["risk"], broker.check_account())
                        except ValueError as exc:
                            snap["error"] = str(exc)
                        ledger.save_baseline(broker.baseline)
                        if ledger.unresolved():
                            snap["error"] = (
                                "Uncertain local operation; operator reconciliation required"
                            )
                        response = client.post("/api/worker/heartbeat", json=snap)
                        response.raise_for_status()
                    ledger.sync(client)
                    # Management commands remain eligible when entries are paused or uncertain.
                    response = client.post("/api/worker/claim")
                    response.raise_for_status()
                    job = response.json()
                    if job:
                        if job["signal"]["action"] == "entry" and (
                            ledger.unresolved() or Path("PAUSE").exists()
                        ):
                            ledger.reserve(job)
                            ledger.finish(
                                job["id"],
                                {
                                    "state": "rejected",
                                    "detail": "Worker paused pending reconciliation",
                                },
                            )
                        else:
                            execute(job, broker, ledger)
                        ledger.sync(client)
                except (httpx.HTTPError, ValueError, sqlite3.Error):
                    logging.exception(
                        "Worker synchronization failed; entries are not resent"
                    )
                tick += 1
                time.sleep(1)
    finally:
        if broker:
            broker.mt.shutdown()
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
