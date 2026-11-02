"""Master-owned communities, subscriber entitlements and isolated child bots."""

import json
import secrets
import time
import uuid
from urllib.parse import urlsplit


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS communities(id TEXT PRIMARY KEY,owner_id TEXT UNIQUE NOT NULL,name TEXT NOT NULL,analyst_id TEXT UNIQUE NOT NULL,invite_token TEXT UNIQUE NOT NULL,access_mode TEXT NOT NULL,monthly_cents INTEGER NOT NULL,stripe_account TEXT,payments_ready INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS community_memberships(community_id TEXT NOT NULL,user_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',blocked INTEGER NOT NULL DEFAULT 0,period_end REAL,customer TEXT,billing_subscription TEXT UNIQUE,expected_cents INTEGER NOT NULL,checkout_key TEXT NOT NULL,checkout_id TEXT,checkout_url TEXT,created REAL NOT NULL,PRIMARY KEY(community_id,user_id));
    CREATE TABLE IF NOT EXISTS community_bots(id TEXT PRIMARY KEY,community_id TEXT NOT NULL,user_id TEXT NOT NULL,account_id TEXT UNIQUE,desired_enabled INTEGER NOT NULL DEFAULT 1,auto_start INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,UNIQUE(community_id,user_id));
    """)

    from .community_discord import migrate as discord_migrate

    discord_migrate(db)


def master_licensed(db, user_id, billing):
    if not billing:
        return True
    user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if user and user["role"] == "admin":
        return True
    sub = db.execute(
        "SELECT * FROM subscriptions WHERE user_id=?", (user_id,)
    ).fetchone()
    return bool(
        sub
        and sub["plan"] == "pro"
        and sub["status"] in ("active", "trialing")
        and (sub["period_end"] or 0) > time.time()
    )


def owned(db, user_id, community_id):
    row = db.execute(
        "SELECT * FROM communities WHERE id=? AND owner_id=?", (community_id, user_id)
    ).fetchone()
    if not row:
        raise ValueError("Community not found")
    return dict(row)


def member_active(row):
    return bool(
        row
        and not row["blocked"]
        and row["status"] in ("active", "trialing", "granted")
        and (row["period_end"] or 0) > time.time()
    )


def entry_access(db, account_id, billing):
    row = db.execute(
        "SELECT b.*,c.owner_id,c.enabled,m.status,m.blocked,m.period_end FROM community_bots b JOIN communities c ON c.id=b.community_id JOIN community_memberships m ON m.community_id=b.community_id AND m.user_id=b.user_id WHERE b.account_id=?",
        (account_id,),
    ).fetchone()
    if not row:
        return None, None
    if not row["enabled"]:
        return False, "Master has paused this community"
    if not master_licensed(db, row["owner_id"], billing):
        return False, "Master software subscription is inactive"
    if not member_active(row):
        return False, "Your subscription to this master is inactive or expired"
    return True, None


def channel_id(value):
    value = value.strip()
    if not value:
        return ""
    if value.isdigit() and len(value) <= 24:
        return value
    parsed = urlsplit(value)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme == "https"
        and parsed.hostname in ("discord.com", "www.discord.com", "discordapp.com")
        and len(parts) == 3
        and parts[0] == "channels"
        and parts[1].isdigit()
        and parts[2].isdigit()
    ):
        return parts[2]
    raise ValueError("Paste a Discord channel link or channel ID")


def create(store, user_id, body, billing):
    now = time.time()
    ident = uuid.uuid4().hex
    analyst = uuid.uuid4().hex
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not master_licensed(db, user_id, billing):
            raise ValueError("Activate a Master software subscription first")
        if db.execute(
            "SELECT 1 FROM communities WHERE owner_id=?", (user_id,)
        ).fetchone():
            raise ValueError("Your master community already exists")
        db.execute(
            "INSERT INTO analysts(id,name,description,guild_id,channel_id,trader_id,active,source_mode) VALUES(?,?,?,'',?,'',1,'broker')",
            (
                analyst,
                body["name"],
                "Private master community",
                "",
            ),
        )
        db.execute(
            "INSERT INTO communities(id,owner_id,name,analyst_id,invite_token,access_mode,monthly_cents,created) VALUES(?,?,?,?,?,?,?,?)",
            (
                ident,
                user_id,
                body["name"],
                analyst,
                secrets.token_urlsafe(24),
                body["access_mode"],
                body["monthly_cents"],
                now,
            ),
        )
        from .community_discord import request_channel

        request_channel(db, ident, user_id, body.get("discord_channel", ""))
        store.event(db, user_id, "community", "Master community created")
    return ident


def invite(store, token):
    with store.db() as db:
        row = db.execute(
            "SELECT id,name,access_mode,monthly_cents,enabled FROM communities WHERE invite_token=?",
            (token,),
        ).fetchone()
        if not row or not row["enabled"]:
            raise ValueError("Invite link is unavailable")
        return dict(row)


def join(store, user_id, token):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        c = db.execute(
            "SELECT * FROM communities WHERE invite_token=? AND enabled=1", (token,)
        ).fetchone()
        if not c:
            raise ValueError("Invite link is unavailable")
        if c["owner_id"] == user_id:
            raise ValueError(
                "This is your master community. Share the link with copy traders."
            )
        db.execute(
            "INSERT OR IGNORE INTO community_memberships(community_id,user_id,expected_cents,checkout_key,created) VALUES(?,?,?,?,?)",
            (c["id"], user_id, c["monthly_cents"], uuid.uuid4().hex, time.time()),
        )
        return c["id"]


def set_access(store, owner_id, community_id, user_id, enabled, days):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        c = owned(db, owner_id, community_id)
        m = db.execute(
            "SELECT * FROM community_memberships WHERE community_id=? AND user_id=?",
            (community_id, user_id),
        ).fetchone()
        if not m:
            raise ValueError("Subscriber not found")
        if enabled:
            if c["access_mode"] == "manual":
                db.execute(
                    "UPDATE community_memberships SET status='granted',blocked=0,period_end=? WHERE community_id=? AND user_id=?",
                    (time.time() + days * 86400, community_id, user_id),
                )
            else:
                if (
                    m["status"] not in ("active", "trialing")
                    or (m["period_end"] or 0) <= time.time()
                ):
                    raise ValueError(
                        "Paid access must be verified by the payment provider first"
                    )
                db.execute(
                    "UPDATE community_memberships SET blocked=0 WHERE community_id=? AND user_id=?",
                    (community_id, user_id),
                )
            ensure_bot(db, community_id, user_id)
            db.execute(
                "UPDATE community_bots SET auto_start=desired_enabled WHERE community_id=? AND user_id=?",
                (community_id, user_id),
            )
        else:
            db.execute(
                "UPDATE community_memberships SET blocked=1 WHERE community_id=? AND user_id=?",
                (community_id, user_id),
            )
            pause_bots(db, community_id, user_id)
        store.event(
            db,
            user_id,
            "subscription",
            "Master access granted (manual)"
            if enabled
            else "Master paused new copying",
        )


def ensure_bot(db, community_id, user_id):
    db.execute(
        "INSERT OR IGNORE INTO community_bots(id,community_id,user_id,account_id,created) VALUES(?,?,?,NULL,?)",
        (uuid.uuid4().hex, community_id, user_id, time.time()),
    )


def pause_bots(db, community_id, user_id):
    db.execute(
        "UPDATE accounts SET enabled=0 WHERE id IN (SELECT account_id FROM community_bots WHERE community_id=? AND user_id=?)",
        (community_id, user_id),
    )
    db.execute(
        "UPDATE pjobs SET state='cancelled' WHERE state='queued' AND json_extract(payload,'$.action')='entry' AND account_id IN (SELECT account_id FROM community_bots WHERE community_id=? AND user_id=?)",
        (community_id, user_id),
    )


def auto_start(store, account_id, billing, allow_live):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute(
            "SELECT * FROM community_bots WHERE account_id=?", (account_id,)
        ).fetchone()
        if not b or not b["auto_start"] or not b["desired_enabled"]:
            return
        ok, _ = entry_access(db, account_id, billing)
        a = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if (
            not ok
            or not a
            or a["disconnected"]
            or (a["mode"] == "live" and not allow_live)
        ):
            return
        if (
            not a["heartbeat"]
            or time.time() - a["heartbeat"] > 10
            or json.loads(a["snapshot"] or "{}").get("error")
        ):
            return
        if db.execute(
            "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('claimed','uncertain')",
            (account_id,),
        ).fetchone():
            return
        db.execute("UPDATE accounts SET enabled=1 WHERE id=?", (account_id,))
        db.execute(
            "UPDATE community_bots SET auto_start=0 WHERE account_id=?", (account_id,)
        )
        store.event(
            db,
            a["user_id"],
            "bot",
            "Your personal copy bot is connected and ready",
            account_id,
        )


def view(store, user_id, billing, base):
    with store.db() as db:
        masters = []
        for raw in db.execute("SELECT * FROM communities WHERE owner_id=?", (user_id,)):
            c = dict(raw)
            c["invite_url"] = base + "/#join/" + c.pop("invite_token")
            c["licensed"] = master_licensed(db, user_id, billing)
            source = db.execute(
                "SELECT s.*,a.name account_name,a.heartbeat,a.disconnected,json_extract(a.snapshot,'$.source_status') source_status FROM master_sources s JOIN accounts a ON a.id=s.account_id WHERE s.analyst_id=?",
                (c["analyst_id"],),
            ).fetchone()
            c["source"] = dict(source) if source else None
            c["discord_channel"] = db.execute(
                "SELECT channel_id FROM analysts WHERE id=?", (c["analyst_id"],)
            ).fetchone()[0]
            c["pending_logs"] = db.execute(
                "SELECT COUNT(*) FROM discord_outbox WHERE analyst_id=? AND state!='sent'",
                (c["analyst_id"],),
            ).fetchone()[0]
            error = db.execute(
                "SELECT last_error FROM discord_outbox WHERE analyst_id=? AND state!='sent' AND last_error IS NOT NULL ORDER BY created DESC LIMIT 1",
                (c["analyst_id"],),
            ).fetchone()
            c["log_error"] = error[0] if error else None
            c["members"] = [
                dict(m)
                for m in db.execute(
                    "SELECT m.user_id,u.name,m.status,m.blocked,m.period_end,b.id bot_id,a.name account_name,a.enabled,a.heartbeat FROM community_memberships m JOIN users u ON u.id=m.user_id LEFT JOIN community_bots b ON b.community_id=m.community_id AND b.user_id=m.user_id LEFT JOIN accounts a ON a.id=b.account_id WHERE m.community_id=? ORDER BY m.created",
                    (c["id"],),
                )
            ]
            masters.append(c)
        memberships = []
        for row in db.execute(
            "SELECT c.id community_id,c.name,c.owner_id,c.enabled,c.access_mode,c.monthly_cents,m.expected_cents,m.status,m.blocked,m.period_end FROM community_memberships m JOIN communities c ON c.id=m.community_id WHERE m.user_id=?",
            (user_id,),
        ):
            m = dict(row)
            m["master_available"] = bool(row["enabled"]) and master_licensed(
                db, m["owner_id"], billing
            )
            m["active"] = member_active(row) and m["master_available"]
            m["access_message"] = (
                "Your master has paused this service. Your subscription can still be managed below."
                if not m["master_available"]
                else "Copying access is paused by your master."
                if row["blocked"]
                else None
            )
            bot = db.execute(
                "SELECT b.*,a.id connected_account_id,a.created connection_created,a.name account_name,a.enabled,a.heartbeat,a.disconnected,a.snapshot FROM community_bots b LEFT JOIN accounts a ON a.id=b.account_id WHERE b.community_id=? AND b.user_id=?",
                (m["community_id"], user_id),
            ).fetchone()
            if bot:
                m["bot"] = {k: v for k, v in dict(bot).items() if k != "snapshot"}
                m["bot"]["account_id"] = bot["connected_account_id"]
                m["bot"]["error"] = json.loads(bot["snapshot"] or "{}").get("error")
            else:
                m["bot"] = None
            memberships.append(m)
        software = db.execute(
            "SELECT plan,status,customer FROM subscriptions WHERE user_id=?", (user_id,)
        ).fetchone()
        return {
            "software_plan": software["plan"] if software else "free",
            "software_status": software["status"] if software else "inactive",
            "masters": masters,
            "memberships": memberships,
            "master_license_active": master_licensed(db, user_id, billing),
            "payments_enabled": billing,
            "server_time": time.time(),
        }
