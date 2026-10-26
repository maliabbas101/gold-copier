from types import SimpleNamespace as Obj
import time

import pytest

from goldcopier.mt5broker import MAGIC, MT5Broker
from goldcopier.signals import parse


class MT:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        self.account = Obj(login=123, server='demo', trade_mode=0, margin_mode=2,
                           equity=10000, trade_allowed=True, trade_expert=True)
        self.tick = Obj(time=time.time(), bid=2700.0, ask=2700.2)
        self.positions = ()
        self.orders = ()
        self.sent = []
        self.result = Obj(retcode=10009, order=1234)

    def initialize(self, **kwargs): return True
    def account_info(self): return self.account
    def terminal_info(self):
        return Obj(connected=True, trade_allowed=True, tradeapi_disabled=False)
    def symbol_select(self, *args): return True
    def symbol_info(self, symbol):
        return Obj(volume_step=.01, volume_min=.01, volume_max=100,
                   trade_stops_level=10, point=.01, digits=2)
    def symbol_info_tick(self, symbol): return self.tick
    def positions_get(self, **kwargs): return self.positions
    def orders_get(self): return self.orders
    def order_calc_profit(self, kind, symbol, volume, price, sl): return -520.0
    def order_check(self, request): return Obj(retcode=0)
    def order_send(self, request):
        self.sent.append(request)
        return self.result


@pytest.fixture
def connected():
    mt = MT()
    cfg = {'terminal_path':'terminal.exe', 'login':'123', 'server':'demo', 'symbol':'XAUUSDm',
           'risk_pct':.5, 'max_lots':1, 'max_spread':.5, 'max_entry_distance':.5,
           'deviation_points':20, 'filling_mode':1}
    return MT5Broker(cfg, mt), mt


def test_request_uses_broker_symbol_and_risk_size(connected):
    broker, mt = connected
    r = broker.prepare(parse('XAUUSD BUY\nENTRY: 2700\nSL: 2695\nTP: 2710\nBE: 2705').asdict(), 'abc')
    assert r['symbol'] == 'XAUUSDm'
    assert r['volume'] == .09
    assert r['sl'] == 2695 and r['tp'] == 2710
    assert broker.send(r)['state'] == 'executed'


@pytest.mark.parametrize('field,value', [('trade_mode',2), ('login',999), ('server','other'),
                                       ('margin_mode',0), ('trade_allowed',False)])
def test_account_switch_and_live_blocked(connected, field, value):
    broker, mt = connected
    setattr(mt.account, field, value)
    with pytest.raises(ValueError): broker.check_account()
    assert mt.sent == []


def test_existing_exposure_blocked(connected):
    broker, mt = connected
    mt.positions = (Obj(ticket=999),)
    with pytest.raises(ValueError):
        broker.prepare(parse('XAUUSD BUY\nENTRY: 2700\nSL: 2695\nTP: 2710\nBE: 2705').asdict(), 'abc')


def test_stale_quote_blocked(connected):
    broker, mt = connected
    mt.tick.time -= 60
    with pytest.raises(ValueError): broker.quote()


def test_partial_fill_is_uncertain(connected):
    broker, mt = connected
    mt.result = Obj(retcode=10010, order=1234)
    assert broker.send({})['state'] == 'uncertain'
    assert len(mt.sent) == 1


@pytest.mark.parametrize('buy', [True, False])
def test_be_actual_fill_and_foreign_positions_untouched(connected, buy):
    broker, mt = connected
    p = Obj(magic=MAGIC, comment='gc:abc', type=0 if buy else 1, price_open=2700.17,
            sl=2695 if buy else 2710, tp=2720 if buy else 2680, symbol='XAUUSDm', ticket=22)
    foreign = Obj(magic=MAGIC, comment='gc:another-job')
    mt.positions = (p, foreign)
    mt.tick.bid = 2706 if buy else 2690
    mt.tick.ask = mt.tick.bid + .2
    broker.breakeven({'comment':'gc:abc'}, 2705 if buy else 2695)
    assert len(mt.sent) == 1
    assert mt.sent[0]['position'] == 22
    assert mt.sent[0]['sl'] == 2700.17 and mt.sent[0]['tp'] == p.tp


def test_be_does_not_loosen_existing_stop(connected):
    broker, mt = connected
    mt.positions = (Obj(magic=MAGIC, comment='gc:abc', type=0, price_open=2700,
                        sl=2702, tp=2710, symbol='XAUUSDm', ticket=22),)
    mt.tick.bid, mt.tick.ask = 2706, 2706.2
    broker.breakeven({'comment':'gc:abc'}, 2705)
    assert not mt.sent
