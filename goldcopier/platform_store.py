import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .auth import digest, password_hash, password_valid
from .platform_models import Risk
from .signals_v2 import parse_signals


def uid():
    return uuid.uuid4().hex


def dump(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


class PlatformStore:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'member',created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,broker TEXT NOT NULL,login TEXT NOT NULL,server TEXT NOT NULL,symbol TEXT NOT NULL,mode TEXT NOT NULL,hosting TEXT NOT NULL,secret TEXT,agent_hash TEXT UNIQUE,enabled INTEGER NOT NULL DEFAULT 0,risk TEXT NOT NULL,snapshot TEXT,heartbeat REAL,created REAL NOT NULL, UNIQUE(server,login));
            CREATE TABLE IF NOT EXISTS analysts(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,guild_id TEXT NOT NULL,channel_id TEXT NOT NULL,trader_id TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS follows(account_id TEXT NOT NULL,analyst_id TEXT NOT NULL,risk TEXT,PRIMARY KEY(account_id,analyst_id));
            CREATE TABLE IF NOT EXISTS feeds(id TEXT PRIMARY KEY,analyst_id TEXT NOT NULL,source_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,UNIQUE(analyst_id,source_id));
            CREATE TABLE IF NOT EXISTS pjobs(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,feed_id TEXT NOT NULL,payload TEXT NOT NULL,risk TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',result TEXT,created REAL NOT NULL,expires REAL NOT NULL,claimed REAL,UNIQUE(account_id,feed_id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,account_id TEXT,kind TEXT NOT NULL,detail TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY AUTOINCREMENT,account_id TEXT NOT NULL,balance REAL NOT NULL,equity REAL NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS subscriptions(user_id TEXT PRIMARY KEY,customer TEXT UNIQUE,subscription TEXT UNIQUE,plan TEXT NOT NULL DEFAULT 'free',status TEXT NOT NULL DEFAULT 'inactive',updated REAL NOT NULL DEFAULT 0,period_end REAL);
            CREATE TABLE IF NOT EXISTS webhook_events(id TEXT PRIMARY KEY,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS resets(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(key TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES('paused','false');
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS equity_account_time ON equity(account_id,created,id)"
            )
            from .community import migrate as migrate_communities

            migrate_communities(db)
            from .instruments import migrate

            migrate(db)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, user_id, kind, detail, account_id=None):
        db.execute(
            "INSERT INTO events(user_id,account_id,kind,detail,created) VALUES(?,?,?,?,?)",
            (user_id, account_id, kind, detail, time.time()),
        )

    def limit(self, key, count=10, window=300):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE created<?", (time.time() - 3600,))
            n = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE key=? AND created>?",
                (key, time.time() - window),
            ).fetchone()[0]
            if n >= count:
                raise ValueError("Too many attempts; try again later")
            db.execute("INSERT INTO attempts VALUES(?,?)", (key, time.time()))

    def signup(self, name, email, password, role="member"):
        user = uid()
        with self.db() as db:
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?)",
                (
                    user,
                    email.lower().strip(),
                    name,
                    password_hash(password),
                    role,
                    time.time(),
                ),
            )
            db.execute("INSERT INTO subscriptions(user_id) VALUES(?)", (user,))
            self.event(db, user, "security", "Account created")
        return user

    def login(self, email, password):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM users WHERE email=?", (email.lower().strip(),)
            ).fetchone()
        # Equalize missing-user password work.
        if not row:
            password_hash(password)
            raise ValueError("Invalid email or password")
        if not password_valid(password, row["password"]):
            raise ValueError("Invalid email or password")
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (digest(token), row["id"], csrf, time.time() + 86400),
            )
        return (
            token,
            csrf,
            {
                "id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "role": row["role"],
            },
        )

    def session(self, token):
        with self.db() as db:
            row = db.execute(
                "SELECT u.id,u.email,u.name,u.role,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?",
                (digest(token), time.time()),
            ).fetchone()
            return dict(row) if row else None

    def account(self, user, account_id, db=None):
        if db is None:
            with self.db() as own:
                return self.account(user, account_id, own)
        row = db.execute(
            "SELECT * FROM accounts WHERE id=? AND user_id=?", (account_id, user)
        ).fetchone()
        if row is None:
            raise ValueError("Account not found")
        return dict(row)

    def add_account(self, user, data, vault, community_id=None):
        account_id = uid()
        analyst_id = data.pop("analyst_id", None)
        purpose = data.get("purpose", "follower")
        if purpose == "master":
            analyst_id = None
        password = data.pop("password")
        if data["hosting"] == "self":
            password = ""
        if data["hosting"] == "managed" and not password:
            raise ValueError("Managed hosting requires the MT5 trading password")
        secret = vault.encrypt(password, account_id) if password else None
        with self.db() as db:
            if analyst_id:
                private = db.execute(
                    "SELECT id FROM communities WHERE analyst_id=?", (analyst_id,)
                ).fetchone()
                if private and private["id"] != community_id:
                    raise ValueError(
                        "Join this master's community and create your personal copy bot there"
                    )
            if (
                analyst_id
                and not db.execute(
                    "SELECT 1 FROM analysts WHERE id=? AND active=1", (analyst_id,)
                ).fetchone()
            ):
                raise ValueError("Choose an active analyst to copy")
            db.execute(
                "INSERT INTO accounts(id,user_id,name,broker,login,server,symbol,mode,hosting,secret,risk,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    user,
                    data["name"],
                    data["broker"],
                    data["login"],
                    data["server"],
                    data["symbol"],
                    data["mode"],
                    data["hosting"],
                    secret,
                    dump(Risk().model_dump()),
                    time.time(),
                ),
            )
            db.execute(
                "UPDATE accounts SET purpose=?,connector=? WHERE id=?",
                (purpose, data.get("connector", "mt5"), account_id),
            )
            pair = (
                "XAUUSD"
                if data["symbol"].startswith("XAUUSD")
                and db.execute(
                    "SELECT 1 FROM pairs WHERE symbol='XAUUSD' AND enabled=1"
                ).fetchone()
                else None
            )
            if pair:
                r = Risk().model_dump()
                db.execute(
                    "INSERT INTO account_pairs VALUES(?,?,?,?,?,?,1)",
                    (
                        account_id,
                        pair,
                        data["symbol"],
                        r["max_spread"],
                        r["max_entry_distance"],
                        r["max_lots"],
                    ),
                )
            if analyst_id:
                db.execute(
                    "INSERT INTO follows(account_id,analyst_id) VALUES(?,?)",
                    (account_id, analyst_id),
                )
            self.event(
                db,
                user,
                "account",
                "MT5 account added; awaiting worker verification",
                account_id,
            )
        return account_id

    def disconnect_account(self, user, account_id, delete=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            a = self.account(user, account_id, db)
            if (
                delete
                and db.execute(
                    "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('claimed','uncertain')",
                    (account_id,),
                ).fetchone()
            ):
                raise ValueError(
                    "Disconnect is available now. Reconcile in-flight commands before deleting this account."
                )
            db.execute(
                "UPDATE accounts SET agent_hash=NULL,secret=NULL,enabled=0,heartbeat=NULL,disconnected=1 WHERE id=?",
                (account_id,),
            )
            db.execute(
                "UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued'",
                (account_id,),
            )
            # A claim may already have reached the worker. Never report it as cancelled.
            db.execute(
                "UPDATE pjobs SET state='uncertain' WHERE account_id=? AND state='claimed'",
                (account_id,),
            )
            if delete:
                db.execute("DELETE FROM follows WHERE account_id=?", (account_id,))
                db.execute(
                    "DELETE FROM account_pairs WHERE account_id=?", (account_id,)
                )
                db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                db.execute(
                    "DELETE FROM master_sources WHERE account_id=?", (account_id,)
                )
                if a["hosting"] == "self":
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES('local_bootstrap_removed','true')"
                    )
            self.event(
                db,
                user,
                "security",
                ("Account deleted" if delete else "Account disconnected")
                + "; credentials and worker token revoked; broker trades remain unchanged",
                account_id,
            )

    def rotate_agent(self, user, account_id):
        token = secrets.token_urlsafe(48)
        with self.db() as db:
            self.account(user, account_id, db)
            db.execute(
                "UPDATE accounts SET agent_hash=?,enabled=0,disconnected=0 WHERE id=?",
                (digest(token), account_id),
            )
            db.execute(
                "UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued'",
                (account_id,),
            )
            db.execute(
                "UPDATE community_bots SET auto_start=desired_enabled WHERE account_id=?",
                (account_id,),
            )
            self.event(
                db, user, "security", "Worker token rotated; copying paused", account_id
            )
        return token

    def agent(self, token):
        if len(token) < 32:
            return None
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE agent_hash=? AND disconnected=0",
                (digest(token),),
            ).fetchone()
            return dict(row) if row else None

    def set_enabled(
        self, user, account_id, enabled, allow_live=False, billing=False, setup=None
    ):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            a = self.account(user, account_id, db)
            guided = db.execute(
                "SELECT guided FROM community_bots WHERE account_id=?", (account_id,)
            ).fetchone()
            needs_setup = bool(guided and guided["guided"])
            if enabled and needs_setup != (setup is not None):
                raise ValueError(
                    "Review your risk on the copy dashboard first"
                    if needs_setup
                    else "Setup is already complete"
                )
            if enabled and a["mode"] == "auto":
                raise ValueError("Wait for the broker to verify your MT5 account")
            if enabled:
                if a.get("purpose") == "master":
                    raise ValueError(
                        "Trader source accounts are read-only and cannot follow signals"
                    )
                if a["disconnected"]:
                    raise ValueError("Reconnect this account before starting copying")
                if a["mode"] == "live" and not allow_live:
                    raise ValueError("Live execution is disabled by the operator")
                if not a["heartbeat"] or time.time() - a["heartbeat"] > 30:
                    raise ValueError("Connect a healthy MT5 worker first")
                snapshot = json.loads(a["snapshot"] or "{}")
                if snapshot.get("error"):
                    raise ValueError("Resolve worker errors before starting")
                if not db.execute(
                    "SELECT 1 FROM follows WHERE account_id=?", (account_id,)
                ).fetchone():
                    raise ValueError("Follow an analyst before starting")
                from .community import entry_access

                entitled, reason = entry_access(db, account_id, billing)
                if entitled is False:
                    raise ValueError(reason)
                if billing and entitled is None:
                    sub = db.execute(
                        "SELECT status,period_end FROM subscriptions WHERE user_id=?",
                        (user,),
                    ).fetchone()
                    if (
                        not sub
                        or sub["status"] not in ("active", "trialing")
                        or not sub["period_end"]
                        or sub["period_end"] < time.time()
                    ):
                        raise ValueError("An active subscription is required")
                if db.execute(
                    "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('uncertain','claimed')",
                    (account_id,),
                ).fetchone():
                    raise ValueError("Reconcile unresolved commands before starting")
            if setup is not None:
                from .community import gold_candidates

                candidates = gold_candidates(snapshot)
                symbol = setup.symbol or (
                    candidates[0] if len(candidates) == 1 else None
                )
                if not candidates or symbol not in candidates:
                    raise ValueError(
                        "Choose a verified gold symbol from your broker account"
                    )
                if not db.execute(
                    "SELECT 1 FROM pairs WHERE symbol='XAUUSD' AND enabled=1"
                ).fetchone():
                    raise ValueError("Gold copying is currently unavailable")
                risk = json.loads(a["risk"])
                risk.update(sizing="percent", risk_pct=setup.risk_pct)
                db.execute(
                    "UPDATE accounts SET symbol=?,risk=? WHERE id=?",
                    (symbol, dump(risk), account_id),
                )
                db.execute(
                    "INSERT INTO account_pairs VALUES(?,?,?,?,?,?,1) ON CONFLICT(account_id,pair) DO UPDATE SET broker_symbol=excluded.broker_symbol,enabled=1",
                    (
                        account_id,
                        "XAUUSD",
                        symbol,
                        risk["max_spread"],
                        risk["max_entry_distance"],
                        risk["max_lots"],
                    ),
                )
                db.execute(
                    "UPDATE community_bots SET guided=0 WHERE account_id=?",
                    (account_id,),
                )
            db.execute(
                "UPDATE accounts SET enabled=? WHERE id=?", (int(enabled), account_id)
            )
            db.execute(
                "UPDATE community_bots SET desired_enabled=?,auto_start=0 WHERE account_id=?",
                (int(enabled), account_id),
            )
            if not enabled:
                db.execute(
                    "UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued' AND json_extract(payload,'$.action')='entry'",
                    (account_id,),
                )
            self.event(
                db,
                user,
                "control",
                "Copying started" if enabled else "New entries paused",
                account_id,
            )

    def set_risk(self, user, account_id, risk, analyst_id=None):
        with self.db() as db:
            self.account(user, account_id, db)
            if analyst_id:
                result = db.execute(
                    "UPDATE follows SET risk=? WHERE account_id=? AND analyst_id=?",
                    (dump(risk), account_id, analyst_id),
                )
                if not result.rowcount:
                    raise ValueError("Follow analyst first")
            else:
                db.execute(
                    "UPDATE accounts SET risk=? WHERE id=?", (dump(risk), account_id)
                )
            self.event(
                db, user, "risk", "Risk settings updated for future entries", account_id
            )

    def follow(self, user, account_id, analyst_id, enabled):
        with self.db() as db:
            a = self.account(user, account_id, db)
            if enabled and a.get("purpose") == "master":
                raise ValueError("A trader source cannot follow or copy its own trades")
            analyst = db.execute(
                "SELECT * FROM analysts WHERE id=? AND active=1", (analyst_id,)
            ).fetchone()
            if not analyst:
                raise ValueError("Analyst not available")
            snap = json.loads(a["snapshot"] or "{}")
            if not enabled:
                if (
                    a["enabled"]
                    or not a["heartbeat"]
                    or time.time() - a["heartbeat"] > 30
                ):
                    raise ValueError(
                        "Pause copying and verify a fresh broker snapshot before unfollowing"
                    )
                if snap.get("positions") or snap.get("orders"):
                    raise ValueError(
                        "Close positions and cancel orders before unfollowing; protection stays active"
                    )
                if db.execute(
                    "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('claimed','uncertain')",
                    (account_id,),
                ).fetchone():
                    raise ValueError("Reconcile in-flight commands before unfollowing")
            child_parent = db.execute(
                "SELECT c.analyst_id FROM community_bots b JOIN communities c ON c.id=b.community_id WHERE b.account_id=?",
                (account_id,),
            ).fetchone()
            if enabled and child_parent and child_parent["analyst_id"] != analyst_id:
                raise ValueError("Your child bot follows its parent master only")
            if enabled:
                private = db.execute(
                    "SELECT id FROM communities WHERE analyst_id=?", (analyst_id,)
                ).fetchone()
                if (
                    private
                    and not db.execute(
                        "SELECT 1 FROM community_bots WHERE account_id=? AND community_id=? AND user_id=?",
                        (account_id, private["id"], user),
                    ).fetchone()
                ):
                    raise ValueError("Use your personal copy bot for this master")
                db.execute(
                    "INSERT OR IGNORE INTO follows(account_id,analyst_id) VALUES(?,?)",
                    (account_id, analyst_id),
                )
            else:
                db.execute(
                    "DELETE FROM follows WHERE account_id=? AND analyst_id=?",
                    (account_id, analyst_id),
                )
            self.event(
                db,
                user,
                "analyst",
                ("Following " if enabled else "Unfollowed ") + analyst["name"],
                account_id,
            )

    def add_analyst(self, data):
        ident = uid()
        with self.db() as db:
            db.execute(
                "INSERT INTO analysts(id,name,description,guild_id,channel_id,trader_id,active) VALUES(?,?,?,?,?,?,?)",
                (
                    ident,
                    data["name"],
                    data["description"],
                    data["guild_id"],
                    data["channel_id"],
                    data["trader_id"],
                    int(data["active"]),
                ),
            )
            db.execute(
                "UPDATE analysts SET source_mode=? WHERE id=?",
                (data.get("source_mode", "discord"), ident),
            )
            self.event(db, None, "admin", "Analyst added: " + data["name"])
        return ident

    def ingest(
        self,
        analyst_id,
        source_id,
        text,
        created,
        ttl=60,
        billing=False,
        reference_id=None,
        origin="discord",
        master_entry=None,
    ):
        from .instruments import canonical

        if not time.time() - ttl <= created <= time.time() + 5:
            raise ValueError("Signal expired or future-dated")
        parsed = parse_signals(text)  # Validate the entire message before any insert.
        if len(parsed) > 1 and len(source_id) > 96:
            raise ValueError("Batch source reference is too long")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[
                0
            ] == "true" and any(item["action"] == "entry" for item in parsed):
                raise ValueError("Platform paused")
            if not db.execute(
                "SELECT 1 FROM analysts WHERE id=? AND active=1", (analyst_id,)
            ).fetchone():
                raise ValueError("Unknown or inactive analyst")
            if (
                db.execute(
                    "SELECT 1 FROM signal_messages WHERE analyst_id=? AND source_id=?",
                    (analyst_id, source_id),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM feeds WHERE analyst_id=? AND source_id=?",
                    (analyst_id, source_id),
                ).fetchone()
            ):
                return {"duplicate": True, "queued": 0, "ids": [], "skipped": []}
            analyst = db.execute(
                "SELECT source_mode FROM analysts WHERE id=?", (analyst_id,)
            ).fetchone()
            if analyst["source_mode"] == "broker" and origin != "broker":
                raise ValueError(
                    "This feed copies the trader account directly. Do not submit Discord/manual signals to it."
                )
            if origin == "broker" and analyst["source_mode"] != "broker":
                raise ValueError("Direct broker source is not enabled")
            if (
                origin == "broker"
                and not db.execute(
                    "SELECT 1 FROM master_sources s JOIN accounts a ON a.id=s.account_id WHERE s.analyst_id=? AND s.enabled=1 AND a.disconnected=0 AND a.purpose='master'",
                    (analyst_id,),
                ).fetchone()
            ):
                raise ValueError("Trader source is paused, disconnected, or unbound")
            from .community import master_licensed

            private_master = db.execute(
                "SELECT owner_id FROM communities WHERE analyst_id=?", (analyst_id,)
            ).fetchone()
            if (
                private_master
                and any(item["action"] == "entry" for item in parsed)
                and not master_licensed(db, private_master["owner_id"], billing)
            ):
                raise ValueError("Master software subscription is inactive")
            payloads = []
            for item in parsed:
                payload = {**item, "origin": "analyst"}
                if payload["action"] == "entry":
                    payload["symbol"] = canonical(db, payload["symbol"])
                    payload["source_group"] = source_id
                    db.execute(
                        "INSERT OR IGNORE INTO signal_threads VALUES(?,?,?,0,?)",
                        (analyst_id, source_id, payload["symbol"], created),
                    )
                    payloads.append(payload)
                    continue
                pair = (
                    canonical(db, payload["symbol"]) if payload.get("symbol") else None
                )
                ref = payload.get("signal_id") or reference_id
                if not ref:
                    threads = db.execute(
                        "SELECT source_id FROM signal_threads WHERE analyst_id=? AND symbol=? AND closed=0",
                        (analyst_id, pair),
                    ).fetchall()
                    if len(threads) != 1:
                        raise ValueError(
                            "Ambiguous or missing original signal. Reply to its Discord message or add SIGNAL: <source message ID>."
                        )
                    ref = threads[0][0]
                refs = db.execute(
                    "SELECT id,payload FROM feeds WHERE analyst_id=? AND json_extract(payload,'$.action')='entry' AND (lower(source_id)=? OR lower(json_extract(payload,'$.source_group'))=?)",
                    (analyst_id, str(ref).lower(), str(ref).lower()),
                ).fetchall()
                refs = [
                    r
                    for r in refs
                    if not pair or json.loads(r["payload"])["symbol"] == pair
                ]
                if (
                    not refs
                    or len({json.loads(r["payload"])["symbol"] for r in refs}) != 1
                ):
                    raise ValueError(
                        "Management reference must identify this analyst's original signal and pair"
                    )
                for reference in refs:
                    original = json.loads(reference["payload"])
                    update = {
                        **payload,
                        "signal_id": reference["id"],
                        "symbol": original["symbol"],
                    }
                    if update.get("targets"):
                        targets = update["targets"]
                        buy = original["side"] == "BUY"
                        edge = (
                            original.get("entry_high")
                            if buy
                            else original.get("entry_low")
                        ) or original["sl"]
                        if (
                            (buy and targets[0] <= edge)
                            or (not buy and targets[0] >= edge)
                            or (
                                len(targets) > 1
                                and (
                                    (buy and targets[1] <= targets[0])
                                    or (not buy and targets[1] >= targets[0])
                                )
                            )
                        ):
                            raise ValueError(
                                "Updated targets must progress beyond the original entry"
                            )
                    if update.get("closed"):
                        group = original.get("source_group", str(ref))
                        db.execute(
                            "UPDATE feeds SET payload=json_set(payload,'$.routing_closed',1) WHERE id=?",
                            (reference["id"],),
                        )
                        still_open = db.execute(
                            "SELECT 1 FROM feeds WHERE analyst_id=? AND json_extract(payload,'$.source_group')=? AND json_extract(payload,'$.symbol')=? AND json_extract(payload,'$.action')='entry' AND COALESCE(json_extract(payload,'$.routing_closed'),0)=0",
                            (analyst_id, group, original["symbol"]),
                        ).fetchone()
                        if not still_open:
                            db.execute(
                                "UPDATE signal_threads SET closed=1 WHERE analyst_id=? AND source_id=? AND symbol=?",
                                (analyst_id, group, original["symbol"]),
                            )
                    payloads.append(update)
            db.execute(
                "INSERT INTO signal_messages VALUES(?,?,?)",
                (analyst_id, source_id, created),
            )
            rows = db.execute(
                "SELECT a.*,f.risk AS follow_risk FROM accounts a JOIN follows f ON f.account_id=a.id WHERE f.analyst_id=? AND a.disconnected=0 AND a.purpose='follower'",
                (analyst_id,),
            ).fetchall()
            if origin == "broker":
                for payload in payloads:
                    payload["broker_source_text"] = text
                    payload["broker_source_id"] = source_id
                    if master_entry is not None:
                        payload["master_entry"] = master_entry
            queued = 0
            ids = []
            skipped = []
            for index, payload in enumerate(payloads):
                feed = uid()
                ids.append(feed)
                ref = source_id if len(payloads) == 1 else f"{source_id}-{index + 1}"
                db.execute(
                    "INSERT INTO feeds VALUES(?,?,?,?,?)",
                    (feed, analyst_id, ref, dump(payload), created),
                )
                for a in rows:
                    if payload["action"] == "notice":
                        self.event(
                            db,
                            a["user_id"],
                            "signal",
                            payload["symbol"]
                            + " analyst status: "
                            + payload["notice"]
                            + " (informational; no broker command)",
                            a["id"],
                        )
                        continue
                    risk = json.loads(a["follow_risk"] or a["risk"])
                    if payload["action"] == "entry":
                        snapshot_error = json.loads(a["snapshot"] or "{}").get("error")
                        reason = None
                        if not a["enabled"]:
                            reason = "Copying is paused"
                        elif not a["heartbeat"] or time.time() - a["heartbeat"] > 30:
                            reason = "Worker is offline or heartbeat is stale"
                        elif snapshot_error:
                            reason = "Worker reports: " + snapshot_error
                        if reason:
                            self.event(
                                db,
                                a["user_id"],
                                "alert",
                                payload["symbol"] + " signal skipped: " + reason,
                                a["id"],
                            )
                            skipped.append(
                                {
                                    "account_id": a["id"],
                                    "pair": payload["symbol"],
                                    "reason": reason,
                                }
                            )
                            continue
                        from .community import entry_access

                        entitled, entitlement_reason = entry_access(
                            db, a["id"], billing
                        )
                        private_feed = db.execute(
                            "SELECT id FROM communities WHERE analyst_id=?",
                            (analyst_id,),
                        ).fetchone()
                        if (
                            private_feed
                            and not db.execute(
                                "SELECT 1 FROM community_bots WHERE account_id=? AND community_id=?",
                                (a["id"], private_feed["id"]),
                            ).fetchone()
                        ):
                            entitled, entitlement_reason = (
                                False,
                                "This account is not a child bot of this master",
                            )
                        if entitled is False:
                            self.event(
                                db,
                                a["user_id"],
                                "subscription",
                                entitlement_reason,
                                a["id"],
                            )
                            skipped.append(
                                {
                                    "account_id": a["id"],
                                    "pair": payload["symbol"],
                                    "reason": entitlement_reason,
                                }
                            )
                            continue
                        if billing and entitled is None:
                            sub = db.execute(
                                "SELECT * FROM subscriptions WHERE user_id=?",
                                (a["user_id"],),
                            ).fetchone()
                            if (
                                not sub
                                or sub["status"] not in ("active", "trialing")
                                or not sub["period_end"]
                                or sub["period_end"] < time.time()
                            ):
                                reason = "Active subscription required"
                                self.event(
                                    db,
                                    a["user_id"],
                                    "alert",
                                    payload["symbol"] + " signal skipped: " + reason,
                                    a["id"],
                                )
                                skipped.append(
                                    {
                                        "account_id": a["id"],
                                        "pair": payload["symbol"],
                                        "reason": reason,
                                    }
                                )
                                continue
                        mapping = db.execute(
                            "SELECT * FROM account_pairs WHERE account_id=? AND pair=? AND enabled=1",
                            (a["id"], payload["symbol"]),
                        ).fetchone()
                        if not mapping:
                            reason = f"No enabled mapping for {payload['symbol']}"
                            self.event(db, a["user_id"], "alert", reason, a["id"])
                            skipped.append(
                                {
                                    "account_id": a["id"],
                                    "pair": payload["symbol"],
                                    "reason": reason,
                                }
                            )
                            continue
                        risk.update(
                            {
                                k: mapping[k]
                                for k in ("max_spread", "max_entry_distance")
                            }
                        )
                        risk["max_lots"] = min(risk["max_lots"], mapping["max_lots"])
                        for key in ("risk_pct", "cash_risk", "fixed_lots"):
                            risk[key] *= payload.get("risk_factor", 1)
                        jobpayload = {
                            **payload,
                            "signal_id": feed,
                            "broker_symbol": mapping["broker_symbol"],
                        }
                    else:
                        original = db.execute(
                            "SELECT payload,state FROM pjobs WHERE account_id=? AND feed_id=? AND json_extract(payload,'$.action')='entry'",
                            (a["id"], payload["signal_id"]),
                        ).fetchone()
                        if not original:
                            continue
                        if original["state"] in ("rejected", "cancelled", "expired"):
                            reason = "Update skipped because the original copied trade did not open."
                            self.event(
                                db,
                                a["user_id"],
                                "signal",
                                f"{payload['symbol']}: {reason}",
                                a["id"],
                            )
                            skipped.append(
                                {
                                    "account_id": a["id"],
                                    "pair": payload["symbol"],
                                    "reason": reason,
                                }
                            )
                            continue
                        original = json.loads(original[0])
                        jobpayload = {
                            **payload,
                            "broker_symbol": original.get("broker_symbol", a["symbol"]),
                        }
                    db.execute(
                        "INSERT INTO pjobs(id,account_id,feed_id,payload,risk,created,expires) VALUES(?,?,?,?,?,?,?)",
                        (
                            uid(),
                            a["id"],
                            feed,
                            dump(jobpayload),
                            dump(risk),
                            time.time(),
                            created + ttl,
                        ),
                    )
                    self.event(
                        db,
                        a["user_id"],
                        "signal",
                        f"{payload['symbol']} signal queued: {payload['action']}",
                        a["id"],
                    )
                    queued += 1
            if origin == "broker":
                from .source_platform import master_feedback

                reasons = "; ".join(dict.fromkeys(item["reason"] for item in skipped))
                master_feedback(
                    self,
                    db,
                    analyst_id,
                    f"Master trade received. Processing {queued} follower(s); {len(skipped)} skipped. "
                    + (
                        reasons
                        or (
                            "Waiting for the follower broker to confirm."
                            if queued
                            else "No follower account is ready to copy this trade."
                        )
                    ),
                )
            return {
                "id": ids[0],
                "ids": ids,
                "queued": queued,
                "duplicate": False,
                "skipped": skipped,
            }

    def control(self, user, account_id, payload):
        with self.db() as db:
            a = self.account(user, account_id, db)
            if a.get("purpose") == "master":
                raise ValueError(
                    "Trader source is read-only; trade on the trader terminal"
                )
            if a["disconnected"]:
                raise ValueError(
                    "Account is disconnected; manage trades directly with your broker"
                )
            ident = uid()
            db.execute(
                "INSERT INTO pjobs(id,account_id,feed_id,payload,risk,created,expires) VALUES(?,?,?,?,?,?,?)",
                (
                    ident,
                    account_id,
                    "manual:" + ident,
                    dump({**payload, "origin": "member"}),
                    a["risk"],
                    time.time(),
                    time.time() + 60,
                ),
            )
            self.event(
                db,
                user,
                "control",
                "Queued " + payload["action"] + " request",
                account_id,
            )
        return ident

    def heartbeat(self, a, snapshot):
        if (
            str(snapshot["login"]) != a["login"]
            or snapshot["server"] != a["server"]
            or (a["mode"] != "auto" and snapshot["mode"] != a["mode"])
        ):
            raise ValueError("Worker account identity mismatch")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT agent_hash,disconnected,snapshot,purpose,mode FROM accounts WHERE id=?",
                (a["id"],),
            ).fetchone()
            if (
                not current
                or current["disconnected"]
                or current["agent_hash"] != a["agent_hash"]
            ):
                raise ValueError("Worker authorization revoked")
            if current["mode"] == "auto":
                db.execute(
                    "UPDATE accounts SET mode=? WHERE id=?", (snapshot["mode"], a["id"])
                )
            elif current["mode"] != snapshot["mode"]:
                raise ValueError("Worker account mode changed")
            db.execute(
                "UPDATE accounts SET snapshot=?,heartbeat=? WHERE id=?",
                (dump(snapshot), time.time(), a["id"]),
            )
            if (
                current["purpose"] == "master"
                and snapshot.get("source_status")
                and snapshot["source_status"]
                != json.loads(current["snapshot"] or "{}").get("source_status")
            ):
                self.event(
                    db, a["user_id"], "source", snapshot["source_status"], a["id"]
                )
            last = db.execute(
                "SELECT MAX(created) FROM equity WHERE account_id=?", (a["id"],)
            ).fetchone()[0]
            if not last or time.time() - last >= 5:
                db.execute(
                    "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
                    (a["id"], snapshot["balance"], snapshot["equity"], time.time()),
                )
            retention_key = "equity_compact:" + a["id"]
            compact = db.execute(
                "SELECT value FROM settings WHERE key=?", (retention_key,)
            ).fetchone()
            if not compact or time.time() - float(compact[0]) >= 3600:
                now = time.time()
                db.execute(
                    "DELETE FROM equity WHERE account_id=? AND created<?",
                    (a["id"], now - 2592000),
                )
                db.execute(
                    "DELETE FROM equity WHERE account_id=? AND created<? AND id NOT IN (SELECT MAX(id) FROM equity WHERE account_id=? AND created<? GROUP BY CAST(created / 60 AS INTEGER))",
                    (a["id"], now - 86400, a["id"], now - 86400),
                )
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES(?,?)",
                    (retention_key, str(now)),
                )
            if snapshot.get("error"):
                db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (a["id"],))
                self.event(db, a["user_id"], "alert", snapshot["error"], a["id"])

    def cancel_failed_followups(self, db, account_id):
        failed = db.execute(
            "SELECT j.* FROM pjobs j JOIN pjobs original ON original.account_id=j.account_id AND original.feed_id=json_extract(j.payload,'$.signal_id') AND json_extract(original.payload,'$.action')='entry' WHERE j.account_id=? AND j.state='queued' AND json_extract(j.payload,'$.action')!='entry' AND json_extract(j.payload,'$.origin')='analyst' AND original.state IN ('rejected','cancelled','expired')",
            (account_id,),
        ).fetchall()
        for job in failed:
            detail = "Update skipped: the original copied trade did not open. No action was sent to the broker."
            db.execute(
                "UPDATE pjobs SET state='cancelled',result=? WHERE id=?",
                (dump({"state": "cancelled", "detail": detail}), job["id"]),
            )
            account = db.execute(
                "SELECT user_id FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            self.event(db, account["user_id"], "signal", detail, account_id)

    def claim(self, a, billing=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM accounts WHERE id=?", (a["id"],)
            ).fetchone()
            if (
                not current
                or current["disconnected"]
                or current["agent_hash"] != a["agent_hash"]
                or current["purpose"] == "master"
            ):
                return None
            db.execute(
                "UPDATE pjobs SET state='expired' WHERE state='queued' AND expires<?",
                (time.time(),),
            )
            paused = (
                db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[
                    0
                ]
                == "true"
            )
            can_enter = bool(current["enabled"]) and not paused
            from .community import entry_access

            entitled, _ = entry_access(db, a["id"], billing)
            if entitled is False:
                can_enter = False
                db.execute(
                    "UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued' AND json_extract(payload,'$.action')='entry'",
                    (a["id"],),
                )
            if billing and entitled is None:
                sub = db.execute(
                    "SELECT * FROM subscriptions WHERE user_id=?", (a["user_id"],)
                ).fetchone()
                can_enter = can_enter and bool(
                    sub
                    and sub["status"] in ("active", "trialing")
                    and sub["period_end"]
                    and sub["period_end"] > time.time()
                )
            db.execute(
                "UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued' AND json_extract(payload,'$.action')='entry' AND NOT EXISTS (SELECT 1 FROM account_pairs m JOIN pairs p ON p.symbol=m.pair WHERE m.account_id=pjobs.account_id AND m.pair=json_extract(pjobs.payload,'$.symbol') AND m.broker_symbol=json_extract(pjobs.payload,'$.broker_symbol') AND m.enabled=1 AND p.enabled=1)",
                (a["id"],),
            )
            self.cancel_failed_followups(db, a["id"])
            unresolved = db.execute(
                "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('claimed','uncertain')",
                (a["id"],),
            ).fetchone()
            clause = (
                ""
                if can_enter and not unresolved
                else " AND json_extract(payload,'$.action')!='entry'"
            )
            row = db.execute(
                "SELECT * FROM pjobs WHERE account_id=? AND state='queued'"
                + " AND NOT (coalesce(json_extract(payload,'$.origin'),'')='analyst' AND json_extract(payload,'$.action')!='entry' AND EXISTS (SELECT 1 FROM pjobs original WHERE original.account_id=pjobs.account_id AND original.feed_id=json_extract(pjobs.payload,'$.signal_id') AND json_extract(original.payload,'$.action')='entry' AND original.state IN ('queued','claimed')))"
                + clause
                + " ORDER BY created LIMIT 1",
                (a["id"],),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE pjobs SET state='claimed',claimed=? WHERE id=?",
                (time.time(), row["id"]),
            )
            return {
                "id": row["id"],
                "signal": json.loads(row["payload"]),
                "risk": json.loads(row["risk"]),
                "expires": row["expires"],
            }

    def result(self, a, job_id, result):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM pjobs WHERE id=? AND account_id=?", (job_id, a["id"])
            ).fetchone()
            if not row:
                raise ValueError("Job not found")
            encoded = dump(result)
            if row["state"] != "claimed":
                if row["result"] == encoded:
                    return
                raise ValueError("Result conflicts with recorded outcome")
            db.execute(
                "UPDATE pjobs SET state=?,result=? WHERE id=?",
                (result["state"], encoded, job_id),
            )
            from .source_platform import enqueue_log, master_feedback
            from .trade_messages import outcome, confirmation

            source_feed = db.execute(
                "SELECT f.* FROM feeds f JOIN analysts n ON n.id=f.analyst_id WHERE f.id=? AND n.source_mode='broker'",
                (row["feed_id"],),
            ).fetchone()
            if source_feed:
                payload = json.loads(source_feed["payload"])
                source_id = payload.get("broker_source_id", source_feed["source_id"])
                readable = outcome(db, source_feed, result, job_id)
                self.event(db, a["user_id"], "execution", readable, a["id"])
                private = readable
                for sensitive in (a.get("login"), a.get("server")):
                    if sensitive:
                        private = private.replace(str(sensitive), "[private]")
                master_feedback(self, db, source_feed["analyst_id"], private)
                if result["state"] == "executed":
                    enqueue_log(
                        db,
                        source_feed["analyst_id"],
                        "source:" + source_feed["analyst_id"] + ":" + source_id,
                        confirmation(db, source_feed),
                        formatted=True,
                    )
            else:
                self.event(db, a["user_id"], "execution", result["detail"], a["id"])
            if result["state"] == "uncertain":
                db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (a["id"],))

    def resolve(self, job_id, state, note, actor):
        with self.db() as db:
            row = db.execute("SELECT * FROM pjobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] not in ("claimed", "uncertain"):
                raise ValueError("Only unresolved jobs may be reconciled")
            # Never requeue or resend. Broker evidence must be reviewed by operator.
            db.execute(
                "UPDATE pjobs SET state=?,result=? WHERE id=?",
                (state, dump({"state": state, "detail": note}), job_id),
            )
            a = db.execute(
                "SELECT user_id FROM accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            self.event(
                db,
                a["user_id"],
                "reconciliation",
                actor + ": " + note,
                row["account_id"],
            )

    def equity_history(self, user, account_id, period="LIVE"):
        periods = {
            "LIVE": (1800, 5),
            "1D": (86400, 120),
            "7D": (604800, 600),
            "30D": (2592000, 3600),
        }
        if period not in periods:
            raise ValueError("Unknown equity period")
        duration, bucket = periods[period]
        with self.db() as db:
            self.account(user, account_id, db)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT e.* FROM equity e JOIN (SELECT MAX(id) id FROM equity WHERE account_id=? AND created>=? GROUP BY CAST(created / ? AS INTEGER)) b ON b.id=e.id ORDER BY e.created,e.id",
                    (account_id, time.time() - duration, bucket),
                )
            ]

    def dashboard(self, user, include_equity=True):
        with self.db() as db:
            accounts = []
            for row in db.execute(
                "SELECT * FROM accounts WHERE user_id=? ORDER BY created", (user,)
            ):
                a = dict(row)
                for key in ("secret", "agent_hash"):
                    a.pop(key, None)
                a["risk"] = json.loads(a["risk"])
                a["snapshot"] = json.loads(a["snapshot"] or "{}")
                a["online"] = bool(
                    not a["disconnected"]
                    and a["heartbeat"]
                    and time.time() - a["heartbeat"] < 30
                )
                a["credential_stored"] = bool(row["secret"])
                source = db.execute(
                    "SELECT s.*,n.name analyst_name FROM master_sources s JOIN analysts n ON n.id=s.analyst_id WHERE s.account_id=?",
                    (a["id"],),
                ).fetchone()
                a["master_source"] = dict(source) if source else None
                child = db.execute(
                    "SELECT id,community_id FROM community_bots WHERE account_id=?",
                    (a["id"],),
                ).fetchone()
                a["child_bot"] = dict(child) if child else None
                a["pairs"] = [
                    dict(m)
                    for m in db.execute(
                        "SELECT * FROM account_pairs WHERE account_id=? ORDER BY pair",
                        (a["id"],),
                    )
                ]
                a["follows"] = [
                    dict(f)
                    for f in db.execute(
                        "SELECT analyst_id,risk FROM follows WHERE account_id=?",
                        (a["id"],),
                    )
                ]
                accounts.append(a)
            analysts = [
                dict(r)
                for r in db.execute(
                    "SELECT n.id,n.name,n.description,n.active,n.source_mode FROM analysts n LEFT JOIN communities c ON c.analyst_id=n.id WHERE c.id IS NULL OR c.owner_id=? OR EXISTS(SELECT 1 FROM community_memberships m WHERE m.community_id=c.id AND m.user_id=?)",
                    (user, user),
                )
            ]
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT j.*,f.source_id source_ref,n.source_mode FROM pjobs j JOIN accounts a ON a.id=j.account_id LEFT JOIN feeds f ON f.id=j.feed_id LEFT JOIN analysts n ON n.id=f.analyst_id WHERE a.user_id=? ORDER BY j.created DESC LIMIT 200",
                    (user,),
                )
            ]
            for j in jobs:
                j["payload"] = json.loads(j["payload"])
                j["result"] = json.loads(j["result"] or "null")
                if j["result"] and j.get("source_mode") == "broker":
                    from .trade_messages import outcome

                    feed = db.execute(
                        "SELECT * FROM feeds WHERE id=?", (j["feed_id"],)
                    ).fetchone()
                    if feed:
                        j["result"]["user_detail"] = outcome(
                            db, feed, j["result"], j["id"]
                        )
                j.pop("risk", None)
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE user_id=? ORDER BY id DESC LIMIT 100",
                    (user,),
                )
            ]
            equity = (
                (
                    [
                        dict(r)
                        for r in db.execute(
                            "SELECT e.* FROM equity e JOIN accounts a ON a.id=e.account_id WHERE a.user_id=? ORDER BY e.created DESC LIMIT 500",
                            (user,),
                        )
                    ][::-1]
                )
                if include_equity
                else []
            )
            sub = db.execute(
                "SELECT plan,status,period_end FROM subscriptions WHERE user_id=?",
                (user,),
            ).fetchone()
            paused = (
                db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[
                    0
                ]
                == "true"
            )
        with self.db() as db:
            from .instruments import registry

            pairs = registry(db)
        with self.db() as db:
            persona = (
                "master"
                if db.execute(
                    "SELECT 1 FROM communities WHERE owner_id=?", (user,)
                ).fetchone()
                else "subscriber"
                if db.execute(
                    "SELECT 1 FROM community_memberships WHERE user_id=?", (user,)
                ).fetchone()
                else "legacy"
            )
        return {
            "persona": persona,
            "pairs": pairs,
            "accounts": accounts,
            "analysts": analysts,
            "jobs": jobs,
            "events": events,
            "equity": equity,
            "subscription": dict(sub),
            "paused": paused,
        }
