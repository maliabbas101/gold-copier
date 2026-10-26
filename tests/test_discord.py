from types import SimpleNamespace as Obj

import pytest

from goldcopier.discord_bot import authorized_message


@pytest.mark.parametrize('change', ['none', 'guild', 'channel', 'trader', 'bot', 'webhook', 'dm'])
def test_only_allowlisted_human_trader(change):
    cfg = {'guild_id':'1','channel_id':'2','trader_id':'3'}
    message = Obj(guild=Obj(id=1), channel=Obj(id=2), author=Obj(id=3, bot=False), webhook_id=None)
    if change == 'guild': message.guild.id = 99
    if change == 'channel': message.channel.id = 99
    if change == 'trader': message.author.id = 99
    if change == 'bot': message.author.bot = True
    if change == 'webhook': message.webhook_id = 99
    if change == 'dm': message.guild = None
    assert authorized_message(message, cfg) is (change == 'none')
