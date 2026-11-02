"""Canonical instrument registry and explicit per-account broker mappings."""

import json
import re
import time
from pydantic import Field, field_validator
from .platform_models import Model

SYMBOL = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"


class PairInput(Model):
    symbol: str = Field(pattern=SYMBOL)
    name: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True

    @field_validator("symbol")
    @classmethod
    def uppercase(cls, value):
        return value.upper()

    @field_validator("aliases")
    @classmethod
    def valid_aliases(cls, values):
        if any(not re.fullmatch(SYMBOL, v) for v in values):
            raise ValueError("Aliases must be single instrument names")
        return sorted(set(v.upper() for v in values))


class MappingInput(Model):
    pair: str = Field(pattern=SYMBOL)
    broker_symbol: str = Field(pattern=SYMBOL)
    max_spread: float = Field(gt=0, le=1e9)
    max_entry_distance: float = Field(gt=0, le=1e9)
    max_lots: float = Field(default=0.1, gt=0, le=10000)
    enabled: bool = True

    @field_validator("pair")
    @classmethod
    def uppercase(cls, value):
        return value.upper()


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS pairs(symbol TEXT PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS pair_aliases(alias TEXT PRIMARY KEY,pair TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS account_pairs(account_id TEXT NOT NULL,pair TEXT NOT NULL,broker_symbol TEXT NOT NULL,max_spread REAL NOT NULL,max_entry_distance REAL NOT NULL,max_lots REAL NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(account_id,pair),UNIQUE(account_id,broker_symbol));
    CREATE TABLE IF NOT EXISTS signal_messages(analyst_id TEXT NOT NULL,source_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(analyst_id,source_id));
    """)
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "CREATE TABLE IF NOT EXISTS signal_threads(analyst_id TEXT NOT NULL,source_id TEXT NOT NULL,symbol TEXT NOT NULL,closed INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,PRIMARY KEY(analyst_id,source_id,symbol))"
    )
    for table, column, definition in (
        ("accounts", "purpose", "TEXT NOT NULL DEFAULT 'follower'"),
        ("analysts", "source_mode", "TEXT NOT NULL DEFAULT 'discord'"),
    ):
        if column not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.execute(
        "CREATE TABLE IF NOT EXISTS master_sources(id TEXT PRIMARY KEY,analyst_id TEXT UNIQUE NOT NULL,account_id TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS discord_outbox(id TEXT PRIMARY KEY,analyst_id TEXT NOT NULL,channel_id TEXT NOT NULL,content TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,last_error TEXT,created REAL NOT NULL,sent REAL)"
    )
    if "disconnected" not in [r[1] for r in db.execute("PRAGMA table_info(accounts)")]:
        db.execute(
            "ALTER TABLE accounts ADD COLUMN disconnected INTEGER NOT NULL DEFAULT 0"
        )
    if not db.execute(
        "SELECT 1 FROM settings WHERE key='pair_registry_seed_v1'"
    ).fetchone():
        db.execute("INSERT OR IGNORE INTO pairs VALUES('XAUUSD','Gold / US dollar',1)")
        db.execute("INSERT OR IGNORE INTO pair_aliases VALUES('XAUUSD','XAUUSD')")
        db.execute("INSERT OR IGNORE INTO pair_aliases VALUES('GOLD','XAUUSD')")
        db.execute("INSERT INTO settings VALUES('pair_registry_seed_v1','done')")
    if "connector" not in [r[1] for r in db.execute("PRAGMA table_info(accounts)")]:
        db.execute(
            "ALTER TABLE accounts ADD COLUMN connector TEXT NOT NULL DEFAULT 'mt5'"
        )
    # One-time legacy migration. Do not recreate deliberately deleted mappings on restart.
    if not db.execute(
        "SELECT 1 FROM settings WHERE key='pair_migration_v1'"
    ).fetchone():
        for a in db.execute("SELECT id,symbol,risk FROM accounts").fetchall():
            if re.fullmatch(r"XAUUSD[A-Za-z0-9._-]*", a["symbol"]):
                r = json.loads(a["risk"])
                db.execute(
                    "INSERT OR IGNORE INTO account_pairs VALUES(?,?,?,?,?,?,1)",
                    (
                        a["id"],
                        "XAUUSD",
                        a["symbol"],
                        r["max_spread"],
                        r["max_entry_distance"],
                        r["max_lots"],
                    ),
                )
        db.execute("INSERT INTO settings VALUES('pair_migration_v1','done')")


def registry(db):
    return [
        {
            **dict(p),
            "aliases": [
                r[0]
                for r in db.execute(
                    "SELECT alias FROM pair_aliases WHERE pair=? AND alias!=? ORDER BY alias",
                    (p["symbol"], p["symbol"]),
                )
            ],
        }
        for p in db.execute("SELECT * FROM pairs ORDER BY symbol")
    ]


def canonical(db, symbol):
    row = db.execute(
        "SELECT p.symbol FROM pair_aliases a JOIN pairs p ON p.symbol=a.pair WHERE a.alias=? AND p.enabled=1",
        (symbol.upper(),),
    ).fetchone()
    if not row:
        raise ValueError(f"Instrument {symbol} is not enabled in the pair registry")
    return row[0]


def busy(db, account_id=None, pair=None):
    query = "SELECT id,snapshot FROM accounts" + (" WHERE id=?" if account_id else "")
    for a in db.execute(query, (account_id,) if account_id else ()):
        mappings = [
            r[0]
            for r in db.execute(
                "SELECT broker_symbol FROM account_pairs WHERE account_id=?"
                + (" AND pair=?" if pair else ""),
                (a["id"], pair) if pair else (a["id"],),
            )
        ]
        snap = json.loads(a["snapshot"] or "{}")
        if any(
            p.get("symbol") in mappings
            for p in snap.get("positions", []) + snap.get("orders", [])
        ):
            return True
        sql = "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('queued','claimed','uncertain')"
        args = [a["id"]]
        if pair:
            sql += " AND (json_extract(payload,'$.symbol')=? OR json_extract(payload,'$.action')!='entry')"
            args.append(pair)
        if db.execute(sql, args).fetchone():
            return True
    return False


def save_pair(store, data, existing=None):
    symbol = data["symbol"]
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM pairs WHERE symbol=?", (existing or symbol,)
        ).fetchone()
        if existing and not old:
            raise ValueError("Pair not found")
        if not existing and old:
            raise ValueError("Pair already exists; edit it instead")
        if existing and existing != symbol:
            raise ValueError("Canonical symbol is immutable; create another pair")
        if old and old["enabled"] and not data["enabled"] and busy(db, pair=symbol):
            raise ValueError(
                "Resolve active exposure and queued instructions before disabling this pair"
            )
        aliases = set([symbol, *data["aliases"]])
        for alias in aliases:
            row = db.execute(
                "SELECT pair FROM pair_aliases WHERE alias=?", (alias,)
            ).fetchone()
            if row and row[0] != symbol:
                raise ValueError(f"Alias {alias} belongs to another pair")
        db.execute(
            "INSERT INTO pairs VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,enabled=excluded.enabled",
            (symbol, data["name"], int(data["enabled"])),
        )
        db.execute("DELETE FROM pair_aliases WHERE pair=?", (symbol,))
        for alias in aliases:
            db.execute("INSERT INTO pair_aliases VALUES(?,?)", (alias, symbol))
        store.event(db, None, "configuration", "Pair saved: " + symbol)


def delete_pair(store, symbol):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM pairs WHERE symbol=?", (symbol,)).fetchone():
            raise ValueError("Pair not found")
        if busy(db, pair=symbol):
            raise ValueError("Pair has active exposure or unresolved instructions")
        db.execute("DELETE FROM account_pairs WHERE pair=?", (symbol,))
        db.execute("DELETE FROM pair_aliases WHERE pair=?", (symbol,))
        db.execute("DELETE FROM pairs WHERE symbol=?", (symbol,))
        store.event(db, None, "configuration", "Pair deleted: " + symbol)


def save_mapping(store, user, account_id, data, existing=None):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        a = store.account(user, account_id, db)
        pair = canonical(db, data["pair"])
        if pair != data["pair"]:
            raise ValueError("Use the canonical pair name for mappings")
        old = db.execute(
            "SELECT * FROM account_pairs WHERE account_id=? AND pair=?",
            (account_id, existing or pair),
        ).fetchone()
        if existing and (not old or existing != pair):
            raise ValueError("Mapping not found or pair changed")
        if old and not existing:
            raise ValueError("Mapping already exists; edit it instead")
        if (
            old
            and (old["broker_symbol"] != data["broker_symbol"] or not data["enabled"])
            and busy(db, account_id, pair)
        ):
            raise ValueError(
                "Resolve exposure and queued instructions before remapping or disabling this pair"
            )
        db.execute(
            "INSERT INTO account_pairs VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,pair) DO UPDATE SET broker_symbol=excluded.broker_symbol,max_spread=excluded.max_spread,max_entry_distance=excluded.max_entry_distance,max_lots=excluded.max_lots,enabled=excluded.enabled",
            (
                account_id,
                pair,
                data["broker_symbol"],
                data["max_spread"],
                data["max_entry_distance"],
                data["max_lots"],
                int(data["enabled"]),
            ),
        )
        store.event(
            db,
            user,
            "configuration",
            f"{pair} mapped to {data['broker_symbol']}",
            account_id,
        )


def delete_mapping(store, user, account_id, pair):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        store.account(user, account_id, db)
        if busy(db, account_id, pair):
            raise ValueError("Mapping has active exposure or unresolved instructions")
        result = db.execute(
            "DELETE FROM account_pairs WHERE account_id=? AND pair=?",
            (account_id, pair),
        )
        if not result.rowcount:
            raise ValueError("Mapping not found")
        store.event(
            db, user, "configuration", "Pair mapping deleted: " + pair, account_id
        )
