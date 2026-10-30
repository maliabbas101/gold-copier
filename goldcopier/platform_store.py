import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .auth import digest, password_hash, password_valid
from .platform_models import Risk
from .signals_v2 import parse_signal


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

    def add_account(self, user, data, vault):
        account_id = uid()
        analyst_id = data.pop("analyst_id", None)
        password = data.pop("password")
        if data["hosting"] == "self":
            password = ""
        if data["hosting"] == "managed" and not password:
            raise ValueError("Managed hosting requires the MT5 trading password")
        secret = vault.encrypt(password, account_id) if password else None
        with self.db() as db:
            if analyst_id and not db.execute(
                "SELECT 1 FROM analysts WHERE id=? AND active=1", (analyst_id,)
            ).fetchone():
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
            if analyst_id:
                db.execute("INSERT INTO follows(account_id,analyst_id) VALUES(?,?)", (account_id, analyst_id))
            self.event(
                db,
                user,
                "account",
                "MT5 account added; awaiting worker verification",
                account_id,
            )
        return account_id

    def rotate_agent(self, user, account_id):
        token = secrets.token_urlsafe(48)
        with self.db() as db:
            self.account(user, account_id, db)
            db.execute(
                "UPDATE accounts SET agent_hash=?,enabled=0 WHERE id=?",
                (digest(token), account_id),
            )
            db.execute(
                "UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued'",
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
                "SELECT * FROM accounts WHERE agent_hash=?", (digest(token),)
            ).fetchone()
            return dict(row) if row else None

    def set_enabled(self, user, account_id, enabled, allow_live=False, billing=False):
        with self.db() as db:
            a = self.account(user, account_id, db)
            if enabled:
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
                if billing:
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
            db.execute(
                "UPDATE accounts SET enabled=? WHERE id=?", (int(enabled), account_id)
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
            if enabled:
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
                "INSERT INTO analysts VALUES(?,?,?,?,?,?,?)",
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
            self.event(db, None, "admin", "Analyst added: " + data["name"])
        return ident

    def ingest(self, analyst_id, source_id, text, created, ttl=60, billing=False):
        if not time.time() - ttl <= created <= time.time() + 5:
            raise ValueError("Signal expired or future-dated")
        payload = {**parse_signal(text), "origin": "analyst"}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[
                    0
                ]
                == "true"
            ):
                raise ValueError("Platform paused")
            if not db.execute(
                "SELECT 1 FROM analysts WHERE id=? AND active=1", (analyst_id,)
            ).fetchone():
                raise ValueError("Unknown or inactive analyst")
            if db.execute(
                "SELECT 1 FROM feeds WHERE analyst_id=? AND source_id=?",
                (analyst_id, source_id),
            ).fetchone():
                return {"duplicate": True, "queued": 0}
            if payload["action"] != "entry":
                reference = db.execute(
                    "SELECT id FROM feeds WHERE analyst_id=? AND lower(source_id)=? AND json_extract(payload,'$.action')='entry'",
                    (analyst_id, payload["signal_id"]),
                ).fetchone()
                if not reference:
                    raise ValueError(
                        "Management reference does not belong to this analyst"
                    )
                payload["signal_id"] = reference["id"]
            # Suppress identical entry reposts during their active ingestion window.
            if (
                payload["action"] == "entry"
                and db.execute(
                    "SELECT 1 FROM feeds WHERE analyst_id=? AND payload=? AND created>?",
                    (analyst_id, dump(payload), time.time() - ttl),
                ).fetchone()
            ):
                return {"duplicate": True, "queued": 0}
            feed = uid()
            db.execute(
                "INSERT INTO feeds VALUES(?,?,?,?,?)",
                (feed, analyst_id, source_id, dump(payload), created),
            )
            rows = db.execute(
                "SELECT a.*,f.risk AS follow_risk FROM accounts a JOIN follows f ON f.account_id=a.id WHERE f.analyst_id=?",
                (analyst_id,),
            ).fetchall()
            queued = 0
            for a in rows:
                if payload["action"] == "entry":
                    if (
                        not a["enabled"]
                        or not a["heartbeat"]
                        or time.time() - a["heartbeat"] > 30
                    ):
                        continue
                    if billing:
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
                            continue
                elif not db.execute(
                    "SELECT 1 FROM pjobs WHERE account_id=? AND feed_id=?",
                    (a["id"], payload["signal_id"]),
                ).fetchone():
                    continue
                jobpayload = (
                    {**payload, "signal_id": feed}
                    if payload["action"] == "entry"
                    else payload
                )
                db.execute(
                    "INSERT INTO pjobs(id,account_id,feed_id,payload,risk,created,expires) VALUES(?,?,?,?,?,?,?)",
                    (
                        uid(),
                        a["id"],
                        feed,
                        dump(jobpayload),
                        a["follow_risk"] or a["risk"],
                        time.time(),
                        created + ttl,
                    ),
                )
                self.event(
                    db,
                    a["user_id"],
                    "signal",
                    "Signal queued: " + payload["action"],
                    a["id"],
                )
                queued += 1
            return {"id": feed, "queued": queued, "duplicate": False}

    def control(self, user, account_id, payload):
        with self.db() as db:
            a = self.account(user, account_id, db)
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
            or snapshot["mode"] != a["mode"]
        ):
            raise ValueError("Worker account identity mismatch")
        with self.db() as db:
            db.execute(
                "UPDATE accounts SET snapshot=?,heartbeat=? WHERE id=?",
                (dump(snapshot), time.time(), a["id"]),
            )
            last = db.execute(
                "SELECT MAX(created) FROM equity WHERE account_id=?", (a["id"],)
            ).fetchone()[0]
            if not last or time.time() - last > 60:
                db.execute(
                    "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
                    (a["id"], snapshot["balance"], snapshot["equity"], time.time()),
                )
            if snapshot.get("error"):
                db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (a["id"],))
                self.event(db, a["user_id"], "alert", snapshot["error"], a["id"])

    def claim(self, a, billing=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM accounts WHERE id=?", (a["id"],)
            ).fetchone()
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
            if billing:
                sub = db.execute(
                    "SELECT * FROM subscriptions WHERE user_id=?", (a["user_id"],)
                ).fetchone()
                can_enter = can_enter and bool(
                    sub
                    and sub["status"] in ("active", "trialing")
                    and sub["period_end"]
                    and sub["period_end"] > time.time()
                )
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

    def dashboard(self, user):
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
                a["online"] = bool(a["heartbeat"] and time.time() - a["heartbeat"] < 30)
                a["credential_stored"] = bool(row["secret"])
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
                for r in db.execute("SELECT id,name,description,active FROM analysts")
            ]
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT j.* FROM pjobs j JOIN accounts a ON a.id=j.account_id WHERE a.user_id=? ORDER BY j.created DESC LIMIT 200",
                    (user,),
                )
            ]
            for j in jobs:
                j["payload"] = json.loads(j["payload"])
                j["result"] = json.loads(j["result"] or "null")
                j.pop("risk", None)
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE user_id=? ORDER BY id DESC LIMIT 100",
                    (user,),
                )
            ]
            equity = [
                dict(r)
                for r in db.execute(
                    "SELECT e.* FROM equity e JOIN accounts a ON a.id=e.account_id WHERE a.user_id=? ORDER BY e.created DESC LIMIT 500",
                    (user,),
                )
            ][::-1]
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
        return {
            "accounts": accounts,
            "analysts": analysts,
            "jobs": jobs,
            "events": events,
            "equity": equity,
            "subscription": dict(sub),
            "paused": paused,
        }
