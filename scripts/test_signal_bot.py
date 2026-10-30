"""Post one test message, or a supplied signal file, then exit."""

import argparse
import os
from pathlib import Path

import httpx

from goldcopier.runtime import load_env
from goldcopier.signals_v2 import parse_signal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signal_file", nargs="?", type=Path,
                        help="Optional plain-text signal file; omitted sends a harmless hello")
    args = parser.parse_args()
    load_env()
    token = os.environ.get("TEST_DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not token or not channel_id.isdigit():
        parser.error("Set TEST_DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID in .env")
    if token == os.environ.get("DISCORD_BOT_TOKEN", "").strip():
        parser.error("Use a separate test bot: the copier ignores its own messages")
    try:
        text = args.signal_file.read_text(encoding="utf-8-sig").strip() if args.signal_file else "Gold Copier test bot is connected."
        if args.signal_file:
            parse_signal(text)
        if len(text) > 2000:
            raise ValueError("Message must be at most 2000 characters")
        with httpx.Client(base_url="https://discord.com/api/v10", timeout=15,
                          headers={"Authorization": "Bot " + token}) as client:
            response = client.get("/users/@me")
            response.raise_for_status()
            bot_id = response.json()["id"]
            print("Test bot ID:", bot_id)
            if args.signal_file and os.environ.get("DISCORD_TRADER_ID") != bot_id:
                raise ValueError("Set DISCORD_TRADER_ID to this test bot ID, restart the server, and select its analyst before sending a signal")
            response = client.get(f"/channels/{channel_id}")
            response.raise_for_status()
            channel = response.json()
            if channel.get("type") not in (0, 5):
                raise ValueError("Choose a server text channel for this test")
            guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
            if guild_id and channel.get("guild_id") != guild_id:
                raise ValueError("The channel does not belong to DISCORD_GUILD_ID")
            response = client.post(f"/channels/{channel_id}/messages", json={
                "content": text, "allowed_mentions": {"parse": []},
            })
            response.raise_for_status()
            print(f"Sent: https://discord.com/channels/{channel['guild_id']}/{channel_id}/{response.json()['id']}")
    except httpx.HTTPError:
        parser.exit(1, "Discord request failed. Check the bot token, channel permissions and connection. Check the channel before retrying; this script does not retry sends.\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Test bot: {exc}\n")


if __name__ == "__main__":
    main()
