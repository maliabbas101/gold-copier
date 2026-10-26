"""Read only. A bot token, never a Discord user token, is required."""
import json
import logging
import os
from pathlib import Path

import discord

from .signals import parse
from .store import Store


def authorized_message(message, cfg):
    return (message.guild is not None and message.webhook_id is None and not message.author.bot
            and str(message.guild.id) == str(cfg['guild_id'])
            and str(message.channel.id) == str(cfg['channel_id'])
            and str(message.author.id) == str(cfg['trader_id']))


def main():
    logging.basicConfig(level=logging.INFO)
    cfg = json.loads(Path(os.environ.get('COPIER_CONFIG', 'config.json')).read_text())
    if not 1 <= cfg.get('signal_ttl_seconds', 60) <= 300:
        raise ValueError('Signal TTL must be between 1 and 300 seconds')
    store = Store(os.environ.get('COPIER_DB', 'copier.db'))
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logging.info('Discord listener connected; no historical messages will be copied')

    @client.event
    async def on_message(message):
        if not authorized_message(message, cfg):
            return
        try:
            signal = parse(message.content)
            accepted = store.ingest(str(message.id), signal, message.created_at.timestamp(),
                                    cfg['accounts'], ttl=cfg.get('signal_ttl_seconds', 60))
            logging.info('Signal %s accepted=%s', message.id, accepted)
        except ValueError as exc:
            logging.warning('Signal %s rejected: %s', message.id, exc)

    # Edits are deliberately not interpreted as new instructions.
    client.run(os.environ['DISCORD_BOT_TOKEN'])


if __name__ == '__main__':
    main()
