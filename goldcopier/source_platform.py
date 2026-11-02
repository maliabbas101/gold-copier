"""Read-only trader source configuration and transactionally queued Discord logs."""

import hashlib
import json
import time
import uuid


def source_config(db, account_id):
    row = db.execute(
        "SELECT s.*,n.name analyst_name,n.active,n.source_mode FROM master_sources s JOIN analysts n ON n.id=s.analyst_id WHERE s.account_id=?",
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def configure_source(store, user_id, body):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        a = store.account(user_id, body["account_id"], db)
        if db.execute(
            "SELECT 1 FROM community_bots WHERE account_id=?", (a["id"],)
        ).fetchone():
            raise ValueError("A subscriber child bot cannot become a master parent bot")
        analyst = db.execute(
            "SELECT * FROM analysts WHERE id=?", (body["analyst_id"],)
        ).fetchone()
        if not analyst:
            raise ValueError("Analyst not found")
        if db.execute(
            "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('claimed','uncertain')",
            (a["id"],),
        ).fetchone():
            raise ValueError(
                "Reconcile follower commands before using this account as a trader source"
            )
        snap = json.loads(a["snapshot"] or "{}")
        if any(
            p.get("copier") for p in snap.get("positions", []) + snap.get("orders", [])
        ):
            raise ValueError(
                "Close copier-owned exposure before converting the account to a trader source"
            )
        if (
            a["purpose"] != "master"
            and db.execute(
                "SELECT 1 FROM pjobs WHERE account_id=? AND state='executed' AND json_extract(payload,'$.action')='entry'",
                (a["id"],),
            ).fetchone()
            and (not a["heartbeat"] or time.time() - a["heartbeat"] > 30)
        ):
            raise ValueError(
                "Verify a fresh follower snapshot before converting an account with previous trades"
            )
        old = db.execute(
            "SELECT * FROM master_sources WHERE analyst_id=? OR account_id=?",
            (analyst["id"], a["id"]),
        ).fetchall()
        if any(
            r["analyst_id"] != analyst["id"] or r["account_id"] != a["id"] for r in old
        ):
            raise ValueError(
                "This trader account or analyst is already bound to another source"
            )
        source_id = old[0]["id"] if old else uuid.uuid4().hex
        db.execute(
            "INSERT INTO master_sources VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled",
            (source_id, analyst["id"], a["id"], int(body["enabled"]), time.time()),
        )
        db.execute(
            "UPDATE analysts SET source_mode='broker' WHERE id=?", (analyst["id"],)
        )
        if body.get("log_channel_id") is not None:
            db.execute(
                "UPDATE analysts SET channel_id=? WHERE id=?",
                (body["log_channel_id"], analyst["id"]),
            )
            if body["log_channel_id"]:
                db.execute(
                    "UPDATE discord_outbox SET channel_id=?,state='pending',next_attempt=0,last_error=NULL WHERE analyst_id=? AND state='not_configured'",
                    (body["log_channel_id"], analyst["id"]),
                )
        db.execute(
            "UPDATE accounts SET purpose='master',enabled=0 WHERE id=?", (a["id"],)
        )
        db.execute("DELETE FROM follows WHERE account_id=?", (a["id"],))
        db.execute(
            "UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued'",
            (a["id"],),
        )
        store.event(
            db,
            user_id,
            "account",
            "Trader source configured; direct copying with asynchronous Discord logs",
            a["id"],
        )
        return source_config(db, a["id"])


def enqueue_log(db, analyst_id, key, content):
    analyst = db.execute(
        "SELECT channel_id FROM analysts WHERE id=?", (analyst_id,)
    ).fetchone()
    if not analyst:
        return
    ident = hashlib.sha256(key.encode()).hexdigest()[:32]
    db.execute(
        "INSERT OR IGNORE INTO discord_outbox(id,analyst_id,channel_id,content,created,state,last_error) VALUES(?,?,?,?,?,?,?)",
        (
            ident,
            analyst_id,
            analyst["channel_id"],
            ("[Gold Copier log]\n" + content)[:1900],
            time.time(),
            "pending" if analyst["channel_id"] else "not_configured",
            None
            if analyst["channel_id"]
            else "Set a Discord log channel in trader source setup to publish this log",
        ),
    )
