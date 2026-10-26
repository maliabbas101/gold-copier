"""One Windows process + one dedicated terminal per consenting demo follower."""
import time

from .risk import size_volume, validate_price

MAGIC = 740126


class MT5Broker:
    def __init__(self, cfg, module=None):
        if module is None:
            import MetaTrader5 as module
        self.mt = module
        self.cfg = cfg
        if not self.mt.initialize(path=cfg['terminal_path']):
            raise RuntimeError('MT5 initialization failed')
        self.check_account()

    def check_account(self):
        a = self.mt.account_info()
        if (a is None or str(a.login) != str(self.cfg['login']) or a.server != self.cfg['server']):
            raise ValueError('Wrong terminal account')
        # v0.1 deliberately enforces demo accounts during integration testing.
        if a.trade_mode != self.mt.ACCOUNT_TRADE_MODE_DEMO:
            raise ValueError('v0.1 only supports MT5 demo accounts')
        if a.margin_mode != self.mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise ValueError('Dedicated hedging account required')
        if not a.trade_allowed or not a.trade_expert:
            raise ValueError('Automated trading disabled on account')
        t = self.mt.terminal_info()
        if t is None or not t.connected or not t.trade_allowed or t.tradeapi_disabled:
            raise ValueError('Terminal not ready for automated trading')
        return a

    def quote(self):
        symbol = self.cfg['symbol']
        if not self.mt.symbol_select(symbol, True):
            raise ValueError('Configured gold symbol unavailable')
        info, tick = self.mt.symbol_info(symbol), self.mt.symbol_info_tick(symbol)
        if info is None or tick is None or abs(time.time() - tick.time) > 10:
            raise ValueError('Missing or stale quote')
        return info, tick

    def prepare(self, signal, job_id):
        a = self.check_account()
        info, tick = self.quote()
        price = validate_price(signal, tick.bid, tick.ask,
                               self.cfg['max_spread'], self.cfg['max_entry_distance'])
        positions = self.mt.positions_get()
        orders = self.mt.orders_get()
        if positions is None or orders is None:
            raise ValueError('Cannot inspect account exposure')
        # One concurrent trade per follower in v0.1; dedicated accounts only.
        if positions or orders:
            raise ValueError('Account has an existing position or pending order')
        buy = signal['side'] == 'BUY'
        kind = self.mt.ORDER_TYPE_BUY if buy else self.mt.ORDER_TYPE_SELL
        loss = self.mt.order_calc_profit(kind, self.cfg['symbol'], 1.0, price, signal['sl'])
        if loss is None or loss >= 0:
            raise ValueError('Cannot calculate stop loss exposure')
        volume = size_volume(a.equity, self.cfg['risk_pct'], abs(loss), info.volume_step,
                             info.volume_min, info.volume_max, self.cfg['max_lots'])
        distance = info.trade_stops_level * info.point
        if abs(price - signal['sl']) < distance or abs(price - signal['tp']) < distance:
            raise ValueError('Stops too close for broker')
        request = {'action': self.mt.TRADE_ACTION_DEAL, 'symbol': self.cfg['symbol'],
                   'type': kind, 'volume': volume, 'price': price,
                   'sl': signal['sl'], 'tp': signal['tp'], 'magic': MAGIC,
                   'comment': 'gc:' + job_id[:20], 'deviation': self.cfg['deviation_points'],
                   'type_time': self.mt.ORDER_TIME_GTC,
                   'type_filling': self.cfg['filling_mode']}
        check = self.mt.order_check(request)
        if check is None or check.retcode != 0:
            raise ValueError('Broker preflight rejected order; inspect terminal journal')
        return request

    def send(self, request):
        result = self.mt.order_send(request)
        if result is None:
            return {'state': 'uncertain', 'detail': 'No broker response; reconcile terminal history'}
        if result.retcode != self.mt.TRADE_RETCODE_DONE:
            # Includes timeout, partial fill, placed, rejection: never automatically resubmit.
            return {'state': 'uncertain', 'detail': f'Broker retcode {result.retcode}; reconcile history'}
        return {'state': 'executed', 'detail': 'Broker confirmed fill', 'ticket': int(result.order)}

    def breakeven(self, request, trigger):
        """Find by copier magic AND job comment. Order ticket is not assumed to be a position ID."""
        self.check_account()
        info, tick = self.quote()
        positions = self.mt.positions_get(symbol=self.cfg['symbol'])
        if positions is None:
            raise ValueError('Cannot inspect BE positions')
        matched = [p for p in positions if p.magic == MAGIC and p.comment == request['comment']]
        if not matched:
            return 'closed-or-unresolved'
        for p in matched:
            buy = p.type == self.mt.ORDER_TYPE_BUY
            if (buy and tick.bid < trigger) or (not buy and tick.ask > trigger):
                continue
            target = round(p.price_open, info.digits)
            if (buy and p.sl >= target) or (not buy and 0 < p.sl <= target):
                continue
            result = self.mt.order_send({'action': self.mt.TRADE_ACTION_SLTP,
                                        'symbol': p.symbol, 'position': p.ticket,
                                        'sl': target, 'tp': p.tp, 'magic': MAGIC})
            if result is None or result.retcode != self.mt.TRADE_RETCODE_DONE:
                raise ValueError('BE modification unconfirmed; will inspect and retry next tick')
        return 'monitored'
