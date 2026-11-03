"""Independent Discord outbox delivery; never in the broker execution path."""

import logging
import os
import time

import httpx
from .platform_store import PlatformStore


def deliver_one(store, client, token):
    if not token:
        return False
    now = time.time()
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM discord_outbox WHERE state IN ('pending','retry','sending') AND next_attempt<=? ORDER BY created,id LIMIT 1",
            (now,),
        ).fetchone()
        if not row:
            return False
        row = dict(row)
        if "\nFollower commands queued:" in row["content"]:
            # Upgrade old queue-only logs without sending a misleading backlog.
            source = row["content"].rsplit("\nSource: ", 1)[-1].split("\n", 1)[0]
            copied = db.execute(
                "SELECT 1 FROM pjobs j JOIN feeds f ON f.id=j.feed_id WHERE f.analyst_id=? AND (f.source_id=? OR json_extract(f.payload,'$.source_group')=?) AND j.state='executed' LIMIT 1",
                (row["analyst_id"], source, source),
            ).fetchone()
            if not copied:
                # A later successful worker acknowledgement recreates this source log.
                db.execute("DELETE FROM discord_outbox WHERE id=?", (row["id"],))
                return True
            feed = db.execute(
                "SELECT f.* FROM feeds f JOIN pjobs j ON j.feed_id=f.id WHERE f.analyst_id=? AND (f.source_id=? OR json_extract(f.payload,'$.source_group')=?) AND j.state='executed' LIMIT 1",
                (row["analyst_id"], source, source),
            ).fetchone()
            from .trade_messages import confirmation

            row["content"] = "[Gold Copier log]\n" + confirmation(db, feed)
            db.execute(
                "UPDATE discord_outbox SET content=? WHERE id=?",
                (row["content"], row["id"]),
            )
        elif "\nSource: " in row["content"]:
            # Format older outbox entries from the previous technical log format.
            source = row["content"].rsplit("\nSource: ", 1)[-1].split("\n", 1)[0]
            feed = db.execute(
                "SELECT f.* FROM feeds f JOIN pjobs j ON j.feed_id=f.id WHERE f.analyst_id=? AND f.source_id=? AND j.state='executed' LIMIT 1",
                (row["analyst_id"], source),
            ).fetchone()
            if not feed:
                db.execute("DELETE FROM discord_outbox WHERE id=?", (row["id"],))
                return True
            from .trade_messages import confirmation

            row["content"] = "[Gold Copier log]\n" + confirmation(db, feed)
            db.execute(
                "UPDATE discord_outbox SET content=? WHERE id=?",
                (row["content"], row["id"]),
            )
        db.execute(
            "UPDATE discord_outbox SET state='sending',attempts=attempts+1,next_attempt=? WHERE id=?",
            (now + 30, row["id"]),
        )
    try:
        response = client.post(
            "https://discord.com/api/v10/channels/" + row["channel_id"] + "/messages",
            headers={"Authorization": "Bot " + token},
            json={
                "content": row["content"],
                "allowed_mentions": {"parse": []},
                "nonce": row["id"][:24],
                "enforce_nonce": True,
            },
        )
        if response.status_code == 429:
            try:
                delay = min(max(float(response.json().get("retry_after", 5)), 1), 300)
            except (ValueError, TypeError):
                delay = 5
            error = "Discord rate limit; will retry asynchronously"
        elif 200 <= response.status_code < 300:
            with store.db() as db:
                db.execute(
                    "UPDATE discord_outbox SET state='sent',sent=?,last_error=NULL WHERE id=?",
                    (time.time(), row["id"]),
                )
            return True
        else:
            delay = min(60, 2 ** min(row["attempts"] + 1, 6))
            error = f"Discord HTTP {response.status_code}; check bot token and channel Send Messages permission"
    except httpx.HTTPError:
        delay = 10
        error = "Discord delivery unavailable; follower execution is unaffected"
    with store.db() as db:
        db.execute(
            "UPDATE discord_outbox SET state='retry',last_error=?,next_attempt=? WHERE id=?",
            (error, time.time() + delay, row["id"]),
        )
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    store = PlatformStore(os.environ.get("PLATFORM_DB", "platform.db"))
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        while True:
            try:
                if not deliver_one(store, client, token):
                    time.sleep(1)
            except Exception:
                logging.exception(
                    "Discord log delivery paused; copier execution continues independently"
                )
                time.sleep(5)


if __name__ == "__main__":
    main()
