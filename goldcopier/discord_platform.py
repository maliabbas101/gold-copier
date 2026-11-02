"""v2 Discord listener. Shares PLATFORM_DB with the web service."""

import logging
import os

import discord

from .platform_store import PlatformStore


def feeds(store, message, listener_id=None):
    if (message.content or "").startswith("[Gold Copier log]"):
        return []
    if (
        not message.guild
        or (message.webhook_id is not None and message.webhook_id != message.author.id)
        or (listener_id is not None and message.author.id == listener_id)
    ):
        return []
    with store.db() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM analysts WHERE guild_id=? AND channel_id=? AND trader_id=? AND active=1 AND source_mode='discord'",
                (
                    str(message.guild.id),
                    str(message.channel.id),
                    str(message.author.id),
                ),
            )
        ]


def ingest(store, message, edit=False, *, billing=False, listener_id=None):
    results = []
    for analyst in feeds(store, message, listener_id):
        text = message.content or ""
        for embed in getattr(message, "embeds", []):
            parts = [getattr(embed, "title", None), getattr(embed, "description", None)]
            parts = [
                p
                for p in parts
                if p and p.strip().upper() not in ("FOREX ALERT", "@FOREX ALERT")
            ]
            parts.extend(f"{f.name}: {f.value}" for f in getattr(embed, "fields", []))
            text += "\n" + "\n".join(part for part in parts if part)
        reference_id = getattr(getattr(message, "reference", None), "message_id", None)
        source = str(message.id)
        created = message.created_at.timestamp()
        if edit:
            # Editing an entry cannot silently create another trade. Only SL/TP changes are propagated.
            from .signals_v2 import parse_signal

            parsed = parse_signal(text)
            if parsed["action"] != "entry":
                continue
            with store.db() as db:
                original = db.execute(
                    "SELECT payload FROM feeds WHERE analyst_id=? AND source_id=?",
                    (analyst["id"], source),
                ).fetchone()
            if not original:
                continue
            import json

            old = json.loads(original[0])
            if any(
                old.get(k) != parsed.get(k)
                for k in (
                    "symbol",
                    "side",
                    "entry",
                    "entry_low",
                    "entry_high",
                    "order_type",
                )
            ):
                logging.warning(
                    "Edited signal changed entry; explicit cancel/new signal is required"
                )
                continue
            if old["sl"] == parsed["sl"] and old["tp"] == parsed["tp"]:
                continue
            if old.get("tp") and not parsed.get("tp"):
                raise ValueError(
                    "Removing targets through an entry edit is unsupported; use explicit management"
                )
            fields = [f"SL: {parsed['sl']}"]
            fields.extend(f"TP{i + 1}: {tp}" for i, tp in enumerate(parsed["targets"]))
            text = f"UPDATE SIGNAL: {source}\n" + "\n".join(fields)
            created = message.edited_at.timestamp()
            source = source + ":edit:" + str(int(created * 1e6))
        try:
            result = store.ingest(
                analyst["id"],
                source,
                text,
                created,
                billing=billing,
                reference_id=reference_id,
            )
        except ValueError as exc:
            # Keep failed actionable alerts visible to followers without copying message content.
            import re

            if re.search(
                r"\b(BUY|SELL|SET TP|BREAKEVEN|STOP LOSS HIT|ENTRY TRIGGERED)\b",
                text,
                re.I,
            ):
                with store.db() as db:
                    for row in db.execute(
                        "SELECT DISTINCT a.user_id,a.id FROM accounts a JOIN follows f ON f.account_id=a.id WHERE f.analyst_id=? AND a.disconnected=0",
                        (analyst["id"],),
                    ).fetchall():
                        store.event(
                            db,
                            row["user_id"],
                            "alert",
                            f"Discord source {source} ignored: {str(exc)[:300]}",
                            row["id"],
                        )
            raise
        logging.info("Discord source %s: %s", source, result)
        results.append(result)
    return results


def main():
    logging.basicConfig(level=logging.INFO)
    store = PlatformStore(os.environ.get("PLATFORM_DB", "platform.db"))
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logging.info("Authorized feeds listener ready; no historical backfill")

    @client.event
    async def on_message(message):
        try:
            ingest(
                store,
                message,
                billing=os.environ.get("BILLING_ENABLED", "false") == "true",
                listener_id=client.user.id if client.user else None,
            )
        except ValueError as exc:
            logging.warning("Signal ignored: %s", exc)

    @client.event
    async def on_message_edit(before, after):
        try:
            ingest(
                store,
                after,
                True,
                billing=os.environ.get("BILLING_ENABLED", "false") == "true",
                listener_id=client.user.id if client.user else None,
            )
        except ValueError as exc:
            logging.warning("Edited signal ignored: %s", exc)

    # Deleting a Discord post never implies closing a financial position.
    client.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
