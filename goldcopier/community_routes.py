"""Nontechnical master and subscriber onboarding; no cross-community authority."""

import json
import os
import time
import uuid

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from . import community as C, platform_models as M
from .source_platform import configure_source
from .platform_store import dump


def install(app, store, vault, billing, base, user, paid, live, demo=False):
    def licensed_owner(current, cid):
        with store.db() as db:
            c = C.owned(db, current["id"], cid)
            if not C.master_licensed(db, current["id"], paid):
                raise ValueError("Activate your Master software subscription first")
            return c

    def account_data(body, purpose):
        if demo:
            raise ValueError("Demo workspaces do not collect broker credentials")
        data = body.model_dump()
        data["purpose"] = purpose
        if not os.environ.get("SUPERVISOR_TOKEN"):
            raise ValueError(
                "Hosted broker connections are not available yet. Your service administrator must finish hosting setup."
            )
        if isinstance(body, M.GuidedMT5):
            return dict(
                data,
                name="My MT5 account",
                broker="Other MT5",
                symbol="XAUUSD",
                mode="auto",
                hosting="managed",
            )
        if isinstance(body, M.ExnessAccountInput):
            from .exness_api import signing_key, host

            if purpose == "follower" and not live:
                raise ValueError(
                    "Live copying is not enabled by this service yet; use a demo MT5 account"
                )
            signing_key(body.private_key)
            return {
                "name": body.name,
                "broker": "Exness",
                "login": body.account_id,
                "server": "Exness API",
                "symbol": "XAUUSD",
                "mode": "live",
                "hosting": "managed_api",
                "connector": "exness",
                "purpose": purpose,
                "password": json.dumps(
                    {
                        "api_key": body.api_key,
                        "private_key": body.private_key,
                        "gateway": host(body.gateway),
                    }
                ),
            }
        if not data["password"]:
            raise ValueError(
                "Enter the broker password so your private bot can connect"
            )
        data["hosting"] = "managed"
        data["analyst_id"] = None
        if purpose == "follower" and body.mode == "live" and not live:
            raise ValueError(
                "Live copying is not enabled by this service yet; use a demo account"
            )
        return data

    @app.get("/api/community")
    def home(current=Depends(user)):
        data = C.view(store, current["id"], paid, base)
        data["managed_available"] = bool(os.environ.get("SUPERVISOR_TOKEN"))
        data["payment_ready"] = bool(paid and billing.key and billing.secret)
        app_id = os.environ.get("DISCORD_APPLICATION_ID", "")
        data["discord_invite_url"] = (
            "https://discord.com/oauth2/authorize?client_id="
            + app_id
            + "&scope=bot&permissions=3072"
            if app_id.isdigit()
            else None
        )
        return data

    @app.post("/api/communities")
    def create(body: M.CommunityInput, current=Depends(user)):
        return {"id": C.create(store, current["id"], body.model_dump(), paid)}

    @app.get("/api/communities/invites/{token}")
    def public_invite(token: str):
        return C.invite(store, token)

    @app.post("/api/communities/invites/{token}/join")
    def join(token: str, current=Depends(user)):
        return {"community_id": C.join(store, current["id"], token)}

    @app.post("/api/communities/{cid}/members/{member_id}/access")
    def access(cid: str, member_id: str, body: M.CommunityGrant, current=Depends(user)):
        licensed_owner(current, cid)
        C.set_access(store, current["id"], cid, member_id, body.enabled, body.days)
        return {"ok": True}

    @app.put("/api/communities/{cid}/channel")
    def change_channel(cid: str, body: M.CommunityChannel, current=Depends(user)):
        licensed_owner(current, cid)
        with store.db() as db:
            from .community_discord import request_channel

            current_channel = C.owned(db, current["id"], cid)[
                "discord_requested_channel"
            ]
            if C.channel_id(body.discord_channel) != (current_channel or ""):
                request_channel(db, cid, current["id"], body.discord_channel)
            if body.discord_role_ids is not None:
                db.execute(
                    "UPDATE communities SET discord_role_ids=? WHERE id=?",
                    (json.dumps(body.discord_role_ids), cid),
                )
        return {"ok": True}

    @app.post("/api/communities/{cid}/discord-connect")
    def discord_connect(cid: str, request: Request, current=Depends(user)):
        licensed_owner(current, cid)
        from .community_discord import DiscordConnection

        return {
            "url": DiscordConnection(store, base).start(
                current["id"], cid, request.cookies.get("gold_session", "")
            )
        }

    @app.get("/api/community-discord/callback")
    def discord_callback(state: str, code: str = ""):
        from .community_discord import DiscordConnection

        try:
            DiscordConnection(store, base).complete(state, code)
        except ValueError:
            # The authenticated dashboard shows the stored error; no provider token is exposed.
            pass
        return RedirectResponse(base + "/#community", status_code=303)

    @app.post("/api/communities/{cid}/enabled")
    def toggle_parent(cid: str, body: M.Toggle, current=Depends(user)):
        licensed_owner(current, cid)
        with store.db() as db:
            db.execute(
                "UPDATE communities SET enabled=? WHERE id=?", (int(body.enabled), cid)
            )
            if body.enabled:
                db.execute(
                    "UPDATE community_bots SET auto_start=desired_enabled WHERE community_id=?",
                    (cid,),
                )
            else:
                for m in db.execute(
                    "SELECT user_id FROM community_memberships WHERE community_id=?",
                    (cid,),
                ).fetchall():
                    C.pause_bots(db, cid, m[0])
        return {"ok": True}

    @app.post("/api/communities/{cid}/source-existing")
    def existing_source(cid: str, body: M.ExistingSource, current=Depends(user)):
        c = licensed_owner(current, cid)
        return configure_source(
            store,
            current["id"],
            {
                "account_id": body.account_id,
                "analyst_id": c["analyst_id"],
                "enabled": True,
                "confirm_read_only": True,
            },
        )

    @app.post("/api/communities/{cid}/source")
    def source(
        cid: str, body: M.AccountInput | M.ExnessAccountInput, current=Depends(user)
    ):
        c = licensed_owner(current, cid)
        with store.db() as db:
            if db.execute(
                "SELECT 1 FROM master_sources WHERE analyst_id=?", (c["analyst_id"],)
            ).fetchone():
                raise ValueError("Your master account is already connected")
        data = account_data(body, "master")
        ident = store.add_account(current["id"], data, vault)
        if isinstance(body, M.ExnessAccountInput):
            with store.db() as db:
                db.execute(
                    "UPDATE accounts SET connector='exness' WHERE id=?", (ident,)
                )
        configure_source(
            store,
            current["id"],
            {
                "account_id": ident,
                "analyst_id": c["analyst_id"],
                "enabled": True,
                "confirm_read_only": True,
            },
        )
        return {"account_id": ident}

    @app.post("/api/communities/{cid}/directory")
    def directory(cid: str, body: M.Toggle, current=Depends(user)):
        licensed_owner(current, cid)
        with store.db() as db:
            db.execute(
                "UPDATE communities SET listed=? WHERE id=?", (int(body.enabled), cid)
            )
        return {"ok": True}

    @app.post("/api/communities/{cid}/start")
    def start(cid: str, body: M.GuidedStart, current=Depends(user)):
        with store.db() as db:
            bot = db.execute(
                "SELECT account_id FROM community_bots WHERE community_id=? AND user_id=?",
                (cid, current["id"]),
            ).fetchone()
        if not bot or not bot["account_id"]:
            raise ValueError("Connect your MT5 account first")
        store.set_enabled(
            current["id"], bot["account_id"], True, live, paid, setup=body
        )
        return {"ok": True}

    @app.post("/api/communities/{cid}/child-bot")
    def child(
        cid: str,
        body: M.GuidedMT5 | M.AccountInput | M.ExnessAccountInput,
        current=Depends(user),
    ):
        guided = isinstance(body, M.GuidedMT5)
        data = account_data(body, "follower")
        account_id = uuid.uuid4().hex
        secret = vault.encrypt(data["password"], account_id)
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            c = db.execute(
                "SELECT * FROM communities WHERE id=? AND enabled=1", (cid,)
            ).fetchone()
            m = db.execute(
                "SELECT * FROM community_memberships WHERE community_id=? AND user_id=?",
                (cid, current["id"]),
            ).fetchone()
            if (
                not c
                or c["owner_id"] == current["id"]
                or not C.member_active(m)
                or not C.master_licensed(db, c["owner_id"], paid)
            ):
                raise ValueError("An active subscription to this master is required")
            C.ensure_bot(db, cid, current["id"])
            bot = db.execute(
                "SELECT * FROM community_bots WHERE community_id=? AND user_id=?",
                (cid, current["id"]),
            ).fetchone()
            if (
                bot["account_id"]
                and db.execute(
                    "SELECT 1 FROM accounts WHERE id=?", (bot["account_id"],)
                ).fetchone()
            ):
                raise ValueError("Your personal bot already has a broker account")
            db.execute(
                "INSERT INTO accounts(id,user_id,name,broker,login,server,symbol,mode,hosting,secret,risk,created,connector,purpose) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    current["id"],
                    data["name"],
                    data["broker"],
                    data["login"],
                    data["server"],
                    data["symbol"],
                    data["mode"],
                    data["hosting"],
                    secret,
                    dump(M.Risk().model_dump()),
                    time.time(),
                    "exness" if isinstance(body, M.ExnessAccountInput) else "mt5",
                    "follower",
                ),
            )
            # Explicit user-entered broker gold symbol; additional pair mappings use the normal owned-account screen.
            if (
                not guided
                and data["symbol"].startswith("XAUUSD")
                and db.execute(
                    "SELECT 1 FROM pairs WHERE symbol='XAUUSD' AND enabled=1"
                ).fetchone()
            ):
                r = M.Risk().model_dump()
                db.execute(
                    "INSERT INTO account_pairs VALUES(?,?,?,?,?,?,1)",
                    (
                        account_id,
                        "XAUUSD",
                        data["symbol"],
                        r["max_spread"],
                        r["max_entry_distance"],
                        r["max_lots"],
                    ),
                )
            db.execute(
                "INSERT INTO follows(account_id,analyst_id) VALUES(?,?)",
                (account_id, c["analyst_id"]),
            )
            db.execute(
                "UPDATE community_bots SET account_id=?,desired_enabled=?,auto_start=?,guided=? WHERE id=?",
                (account_id, int(not guided), int(not guided), int(guided), bot["id"]),
            )
            store.event(
                db,
                current["id"],
                "bot",
                "Personal child bot generated; broker connection is starting",
                account_id,
            )
        return {"bot_id": bot["id"], "account_id": account_id}

    @app.post("/api/communities/{cid}/checkout")
    def checkout(cid: str, current=Depends(user)):
        from .community_billing import CommunityBilling

        return {"url": CommunityBilling(billing).checkout(current, cid)}

    @app.post("/api/communities/{cid}/payments/onboard")
    def onboard(cid: str, current=Depends(user)):
        licensed_owner(current, cid)
        from .community_billing import CommunityBilling

        return {"url": CommunityBilling(billing).onboard(current, cid)}

    @app.post("/api/communities/{cid}/payments/refresh")
    def refresh_payments(cid: str, current=Depends(user)):
        licensed_owner(current, cid)
        from .community_billing import CommunityBilling

        return CommunityBilling(billing).refresh(current, cid)

    @app.post("/api/communities/{cid}/billing-portal")
    def portal(cid: str, current=Depends(user)):
        from .community_billing import CommunityBilling

        return {"url": CommunityBilling(billing).portal(current, cid)}
