"""Authorize each master's Discord destination instead of trusting a pasted channel ID."""

import hashlib
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx


def migrate(db):
    columns = {r[1] for r in db.execute("PRAGMA table_info(communities)")}
    for name in (
        "discord_requested_channel",
        "discord_requested_guild",
        "discord_verified_guild",
        "discord_user_id",
        "discord_error",
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE communities ADD COLUMN {name} TEXT")
    if "discord_role_ids" not in columns:
        db.execute(
            "ALTER TABLE communities ADD COLUMN discord_role_ids TEXT NOT NULL DEFAULT '[]'"
        )
    db.execute(
        "CREATE TABLE IF NOT EXISTS community_discord_states(digest TEXT PRIMARY KEY,community_id TEXT NOT NULL,user_id TEXT NOT NULL,session_digest TEXT NOT NULL,expires REAL NOT NULL)"
    )


def request_channel(db, community_id, user_id, value):
    from .community import channel_id

    channel = channel_id(value)
    parts = urlsplit(value).path.strip("/").split("/")
    guild = (
        parts[1]
        if len(parts) == 3 and parts[0] == "channels" and parts[1].isdigit()
        else None
    )
    c = db.execute(
        "SELECT analyst_id FROM communities WHERE id=? AND owner_id=?",
        (community_id, user_id),
    ).fetchone()
    if not c:
        raise ValueError("Community not found")
    role = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()[0]
    trusted = (
        role == "admin"
    )  # Platform administrators retain the existing operator-managed setup path.
    db.execute(
        "UPDATE communities SET discord_requested_channel=?,discord_requested_guild=?,discord_error=NULL WHERE id=?",
        (channel, guild, community_id),
    )
    db.execute(
        "UPDATE analysts SET channel_id=? WHERE id=?",
        (channel if trusted else "", c["analyst_id"]),
    )
    db.execute(
        "UPDATE discord_outbox SET channel_id=?,state=?,last_error=?,next_attempt=0 WHERE analyst_id=? AND state!='sent'",
        (
            channel if trusted else "",
            "pending" if trusted and channel else "not_configured",
            None
            if trusted and channel
            else "Connect and authorize the Discord output channel",
            c["analyst_id"],
        ),
    )


class DiscordConnection:
    def __init__(self, store, base, client=None):
        self.store, self.base = store, base
        self.client_id = os.environ.get("DISCORD_APPLICATION_ID", "")
        self.secret = os.environ.get("DISCORD_CLIENT_SECRET", "")
        self.bot = os.environ.get("DISCORD_BOT_TOKEN", "")
        self.client = client

    def start(self, user_id, cid, session):
        from .community import owned

        if not self.client_id or not self.secret or not self.bot:
            raise ValueError("Discord connection is awaiting service configuration")
        state = secrets.token_urlsafe(32)
        with self.store.db() as db:
            c = owned(db, user_id, cid)
            if not c["discord_requested_channel"]:
                raise ValueError("Choose your Discord output channel first")
            db.execute(
                "DELETE FROM community_discord_states WHERE expires<?", (time.time(),)
            )
            db.execute(
                "INSERT INTO community_discord_states VALUES(?,?,?,?,?)",
                (
                    hashlib.sha256(state.encode()).hexdigest(),
                    cid,
                    user_id,
                    hashlib.sha256(session.encode()).hexdigest(),
                    time.time() + 600,
                ),
            )
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "identify guilds bot",
            "permissions": "3072",
            "redirect_uri": self.base + "/api/community-discord/callback",
            "state": state,
        }
        if c["discord_requested_guild"]:
            params["guild_id"] = c["discord_requested_guild"]
        return "https://discord.com/oauth2/authorize?" + urlencode(params)

    def complete(self, state, code):
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        with self.store.db() as db:
            row = db.execute(
                "SELECT o.* FROM community_discord_states o JOIN sessions s ON s.token=o.session_digest AND s.user_id=o.user_id WHERE o.digest=? AND o.expires>? AND s.expires>?",
                (state_hash, time.time(), time.time()),
            ).fetchone()
            if not row:
                raise ValueError(
                    "Discord connection expired or session was revoked. Start again from your community."
                )
            row = dict(row)
            c = db.execute(
                "SELECT * FROM communities WHERE id=? AND owner_id=?",
                (row["community_id"], row["user_id"]),
            ).fetchone()
            if not c:
                raise ValueError("Community not found")
            c = dict(c)
        client = self.client or httpx.Client(timeout=15, follow_redirects=False)
        try:
            response = client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.base + "/api/community-discord/callback",
                },
            )
            response.raise_for_status()
            token = response.json()["access_token"]
            headers = {"Authorization": "Bearer " + token}
            me = client.get("https://discord.com/api/v10/users/@me", headers=headers)
            me.raise_for_status()
            channel = client.get(
                "https://discord.com/api/v10/channels/"
                + c["discord_requested_channel"],
                headers={"Authorization": "Bot " + self.bot},
            )
            channel.raise_for_status()
            channel = channel.json()
            guild = channel.get("guild_id")
            if not guild or channel.get("type") not in (0, 5):
                raise ValueError("Choose a server text or announcement channel")
            if c["discord_requested_guild"] and guild != c["discord_requested_guild"]:
                raise ValueError(
                    "Discord channel does not belong to the selected server"
                )
            allowed = False
            after = None
            for _ in range(5):
                params = {"limit": 200}
                if after:
                    params["after"] = after
                response = client.get(
                    "https://discord.com/api/v10/users/@me/guilds",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                guilds = response.json()
                match = next((g for g in guilds if g["id"] == guild), None)
                if match:
                    allowed = bool(
                        match.get("owner")
                        or int(match.get("permissions", "0")) & (8 | 32)
                    )
                    break
                if len(guilds) < 200:
                    break
                after = guilds[-1]["id"]
            if not allowed:
                raise ValueError(
                    "You must own or manage the Discord server that receives your community signals"
                )
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute(
                    "SELECT o.* FROM community_discord_states o JOIN sessions s ON s.token=o.session_digest AND s.user_id=o.user_id WHERE o.digest=? AND o.expires>? AND s.expires>?",
                    (state_hash, time.time(), time.time()),
                ).fetchone()
                current = db.execute(
                    "SELECT * FROM communities WHERE id=? AND owner_id=?",
                    (c["id"], row["user_id"]),
                ).fetchone()
                if (
                    not fresh
                    or not current
                    or current["discord_requested_channel"]
                    != c["discord_requested_channel"]
                ):
                    raise ValueError(
                        "Discord destination changed; reconnect from the community dashboard"
                    )
                db.execute(
                    "UPDATE communities SET discord_verified_guild=?,discord_user_id=?,discord_error=NULL WHERE id=?",
                    (guild, me.json()["id"], c["id"]),
                )
                db.execute(
                    "UPDATE analysts SET channel_id=? WHERE id=?",
                    (channel["id"], c["analyst_id"]),
                )
                db.execute(
                    "UPDATE discord_outbox SET channel_id=?,state='pending',next_attempt=0,last_error=NULL WHERE analyst_id=? AND state='not_configured'",
                    (channel["id"], c["analyst_id"]),
                )
                db.execute(
                    "DELETE FROM community_discord_states WHERE digest=?", (state_hash,)
                )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            with self.store.db() as db:
                db.execute(
                    "DELETE FROM community_discord_states WHERE digest=?", (state_hash,)
                )
                db.execute(
                    "UPDATE communities SET discord_error=? WHERE id=?",
                    (
                        "Discord authorization did not complete. Choose a channel in a server you manage and allow the bot to send messages.",
                        c["id"],
                    ),
                )
            raise ValueError(
                "Discord channel authorization failed. Return to your community dashboard to retry."
            ) from exc
        finally:
            if not self.client:
                client.close()
