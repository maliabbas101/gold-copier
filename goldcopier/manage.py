"""Operator CLI: key generation, admin creation, and isolated demo seeding."""

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import secrets
import time

from .auth import Vault
from .platform_store import PlatformStore, dump, uid


def seed_demo(store, password):
    if os.environ.get("DEMO_MODE") != "true":
        raise ValueError("Set DEMO_MODE=true for seeded demo data")
    with store.db() as db:
        if db.execute("SELECT 1 FROM users").fetchone():
            raise ValueError("Demo seeding requires a fresh empty database")
    user = store.signup("Alex Morgan", "demo@goldcopier.local", password, "admin")
    analyst = store.add_analyst(
        {
            "name": "Community Gold Desk",
            "description": "A sample gold analyst feed for exploring account controls and execution history. This is illustrative data.",
            "guild_id": "10001",
            "channel_id": "10002",
            "trader_id": "10003",
            "active": True,
        }
    )
    other = store.add_analyst(
        {
            "name": "London Session",
            "description": "Sample session-focused analyst. Follow and risk settings are available for review.",
            "guild_id": "10001",
            "channel_id": "10004",
            "trader_id": "10005",
            "active": True,
        }
    )
    vault = Vault(os.environ["VAULT_KEY"])
    account = store.add_account(
        user,
        {
            "name": "Gold portfolio",
            "broker": "Exness",
            "login": "80012345",
            "server": "Sample-Demo",
            "symbol": "XAUUSDm",
            "mode": "demo",
            "hosting": "self",
            "password": "",
        },
        vault,
    )
    store.follow(user, account, analyst, True)
    now = time.time()
    snapshot = {
        "login": "80012345",
        "server": "Sample-Demo",
        "mode": "demo",
        "balance": 5482.50,
        "equity": 5508.40,
        "currency": "USD",
        "daily_pnl": 82.50,
        "positions": [
            {
                "ticket": 7100231,
                "symbol": "XAUUSDm",
                "side": "BUY",
                "volume": 0.02,
                "price_open": 2700.20,
                "sl": 2694,
                "tp": 2720,
                "profit": 29.60,
                "copier": True,
            },
            {
                "ticket": 7100208,
                "symbol": "XAUUSDm",
                "side": "SELL",
                "volume": 0.01,
                "price_open": 2704.50,
                "sl": 2711,
                "tp": 2690,
                "profit": -3.70,
                "copier": True,
            },
        ],
        "orders": [],
    }
    with store.db() as db:
        db.execute(
            "UPDATE accounts SET snapshot=?,heartbeat=? WHERE id=?",
            (dump(snapshot), now, account),
        )
        points = [
            5000,
            5038,
            5012,
            5100,
            5075,
            5190,
            5140,
            5168,
            5250,
            5230,
            5328,
            5297,
            5360,
            5344,
            5426,
            5402,
            5480,
            5463,
            5508.4,
        ]
        for i, value in enumerate(points):
            db.execute(
                "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
                (
                    account,
                    value - 12,
                    value,
                    now - 6 * 86400 + i * 6 * 86400 / (len(points) - 1),
                ),
            )
        for i, (kind, detail) in enumerate(
            [
                ("execution", "Sample: BUY order confirmed by broker"),
                ("risk", "Risk per trade set to 0.5%"),
                ("execution", "Sample: breakeven stop adjustment confirmed"),
                ("signal", "Sample: XAUUSD signal received"),
                ("account", "Sample: MT5 terminal connected"),
            ]
        ):
            db.execute(
                "INSERT INTO events(user_id,account_id,kind,detail,created) VALUES(?,?,?,?,?)",
                (user, account, kind, detail, now - (i + 1) * 190),
            )
        for i, state in enumerate(["executed", "executed", "rejected", "executed"]):
            job = uid()
            payload = {
                "action": "entry",
                "symbol": "XAUUSD",
                "side": "BUY" if i % 2 == 0 else "SELL",
                "order_type": "MARKET",
                "signal_id": uid(),
            }
            result = {
                "state": state,
                "detail": "Sample broker confirmation"
                if state == "executed"
                else "Sample: spread above configured limit",
                "ticket": 7100100 + i,
            }
            db.execute(
                "INSERT INTO pjobs(id,account_id,feed_id,payload,risk,state,result,created,expires,claimed) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job,
                    account,
                    "seed:" + job,
                    dump(payload),
                    "{}",
                    state,
                    dump(result),
                    now - (i + 1) * 3600,
                    now - i * 3600,
                    now - (i + 1) * 3600,
                ),
            )
    print("Demo workspace created. Email: demo@goldcopier.local")
    print(
        "Password: use the DEMO_PASSWORD you supplied. These are sample records; execution is disabled."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["run", "key", "create-admin", "seed-demo", "backup", "worker-token"],
    )
    parser.add_argument("--account", help="account ID for worker-token")
    parser.add_argument("--email")
    parser.add_argument("--name", default="Community Operator")
    parser.add_argument("--output")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    from .runtime import load_env

    if args.command == "run":
        from .runtime import main as run

        try:
            run(args.env_file)
        except (ValueError, OSError) as exc:
            parser.exit(1, f"Startup failed: {exc}\n")
        return
    load_env(args.env_file)
    if args.command == "key":
        print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        return
    state = Path(os.environ.get("LOCAL_STATE_DIR") or ".local-runtime")
    store = PlatformStore(os.environ.get("PLATFORM_DB") or state / "platform.db")
    if args.command == "worker-token":
        # Offline provisioning for a standalone platform_agent; no server needed.
        with store.db() as db:
            rows = db.execute(
                "SELECT id,user_id,login,server FROM accounts WHERE hosting='self'"
            ).fetchall()
        rows = [r for r in rows if not args.account or r["id"] == args.account]
        if not rows:
            parser.exit(1, "No self-hosted account found. Start manage run once first.\n")
        if len(rows) > 1:
            parser.exit(
                1,
                "Several self-hosted accounts; choose one with --account: "
                + ", ".join(r["id"] for r in rows)
                + "\n",
            )
        row = rows[0]
        token = store.rotate_agent(row["user_id"], row["id"])
        print(f"Worker token for MT5 {row['login']} on {row['server']} (copying paused).")
        print("Put this line in the worker's .env:")
        print("AGENT_TOKEN=" + token)
        return
    if args.command == "backup":
        if not args.output:
            raise ValueError("--output is required")
        import sqlite3

        target = Path(args.output)
        if target.exists():
            raise ValueError("Backup target already exists")
        with store.db() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        print("Database backup created")
        return
    password = os.environ.get(
        "DEMO_PASSWORD" if args.command == "seed-demo" else "ADMIN_PASSWORD"
    ) or getpass.getpass("Password (at least 12 characters): ")
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    if args.command == "seed-demo":
        seed_demo(store, password)
    elif not args.email:
        raise ValueError("--email is required")
    else:
        store.signup(args.name, args.email, password, "admin")
        print("Operator account created")


if __name__ == "__main__":
    main()
