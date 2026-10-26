"""MT5 execution for platform workers. All state-changing requests are preflighted.

Broker comment matching is checked against returned positions and deal history.
No uncertain entry or partial-close instruction is automatically resent.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import math
import time

from .mt5broker import MT5Broker
from .platform_models import Risk

MAGIC = 740127


def floor_lots(volume, step):
    return float(
        (Decimal(str(volume)) / Decimal(str(step))).to_integral_value(
            rounding=ROUND_DOWN
        )
        * Decimal(str(step))
    )


class PlatformBroker(MT5Broker):
    def __init__(self, cfg, module=None):
        self.baseline = cfg.setdefault("baseline", {})
        super().__init__(cfg, module)

    def check_account(self):
        a = self.mt.account_info()
        if (
            a is None
            or str(a.login) != str(self.cfg["login"])
            or a.server != self.cfg["server"]
        ):
            raise ValueError("Worker is connected to the wrong broker account")
        expected = self.cfg.get("mode", "demo")
        actual = "demo" if a.trade_mode == self.mt.ACCOUNT_TRADE_MODE_DEMO else "live"
        if actual != expected:
            raise ValueError("Account trade mode does not match registration")
        if actual == "live" and not self.cfg.get("allow_live", False):
            raise ValueError(
                "Live execution requires both operator and local worker enablement"
            )
        if a.margin_mode != self.mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise ValueError("A dedicated hedging account is required")
        if not a.trade_allowed or not a.trade_expert:
            raise ValueError("Broker trading permissions are disabled")
        terminal = self.mt.terminal_info()
        if (
            terminal is None
            or not terminal.connected
            or not terminal.trade_allowed
            or terminal.tradeapi_disabled
        ):
            raise ValueError("Terminal is not ready for automated trading")
        return a

    def exposure(self, risk, a):
        positions = self.mt.positions_get()
        orders = self.mt.orders_get()
        if positions is None or orders is None:
            raise ValueError("Cannot verify existing account exposure")
        if len(positions) + len(orders) >= risk["max_open"]:
            raise ValueError("Maximum concurrent exposure reached")
        used = 0.0
        for p in (*positions, *orders):
            if not p.sl:
                raise ValueError("Existing exposure has no stop loss")
            kind = (
                self.mt.ORDER_TYPE_BUY
                if p.type in (0, 2, 4, 6)
                else self.mt.ORDER_TYPE_SELL
            )
            volume = getattr(p, "volume", None) or getattr(p, "volume_current", None)
            loss = self.mt.order_calc_profit(kind, p.symbol, volume, p.price_open, p.sl)
            if loss is None or not math.isfinite(loss):
                raise ValueError("Cannot price existing stop exposure")
            used += max(0, -loss)
        return used

    def prepare_entry(self, signal, risk, job_id):
        risk = Risk(**risk).model_dump()
        a = self.check_account()
        info, tick = self.quote()
        if tick.ask < tick.bid or tick.ask - tick.bid > risk["max_spread"]:
            raise ValueError("Spread exceeds maximum")
        buy = signal["side"] == "BUY"
        kind = self.mt.ORDER_TYPE_BUY if buy else self.mt.ORDER_TYPE_SELL
        market = tick.ask if buy else tick.bid
        pending = signal["order_type"] != "MARKET"
        price = signal["entry"] if pending else market
        if (
            not pending
            and not signal["entry_low"] - risk["max_entry_distance"]
            <= price
            <= signal["entry_high"] + risk["max_entry_distance"]
        ):
            raise ValueError("Price moved outside entry tolerance")
        if (buy and not signal["sl"] < price < signal["be"] <= signal["tp"]) or (
            not buy and not signal["tp"] <= signal["be"] < price < signal["sl"]
        ):
            raise ValueError("Price no longer supports signal protection")
        if pending:
            limit = signal["order_type"] == "LIMIT"
            if (
                (buy and limit and price >= tick.ask)
                or (buy and not limit and price <= tick.ask)
                or (not buy and limit and price <= tick.bid)
                or (not buy and not limit and price >= tick.bid)
            ):
                raise ValueError("Pending entry is on the wrong side of market")
        self.protection(risk, a)
        used = self.exposure(risk, a)
        loss = self.mt.order_calc_profit(
            kind, self.cfg["symbol"], 1.0, price, signal["sl"]
        )
        if loss is None or not math.isfinite(loss) or loss >= 0:
            raise ValueError("Cannot calculate broker stop-loss risk")
        cap = a.equity * risk["risk_pct"] / 100
        budget = min(cap, risk["cash_risk"]) if risk["sizing"] == "cash" else cap
        raw = risk["fixed_lots"] if risk["sizing"] == "fixed" else budget / abs(loss)
        volume = floor_lots(
            min(raw, risk["max_lots"], info.volume_max), info.volume_step
        )
        if volume < info.volume_min:
            raise ValueError("Minimum broker lot exceeds budget")
        estimated = abs(loss) * volume
        if (
            estimated > cap + 1e-8
            or estimated + used > a.equity * risk["total_risk_pct"] / 100 + 1e-8
        ):
            raise ValueError("Trade exceeds per-trade or aggregate risk cap")
        if risk["sizing"] == "cash" and estimated > risk["cash_risk"] + 1e-8:
            raise ValueError("Trade exceeds cash risk budget")
        distance = info.trade_stops_level * info.point
        if abs(price - signal["sl"]) < distance or abs(price - signal["tp"]) < distance:
            raise ValueError("Stops are too close for this broker")
        # For multi-target signals TP exits are journaled software operations.
        # A 100% TP allocation can keep a broker-side final TP as an additional protection.
        runner = (
            signal["multi_target"]
            and sum(
                [risk["tp1_close_pct"], risk["tp2_close_pct"]][: len(signal["targets"])]
            )
            < 100
        )
        tp = 0 if runner else signal["tp"]
        request = {
            "action": self.mt.TRADE_ACTION_PENDING
            if pending
            else self.mt.TRADE_ACTION_DEAL,
            "symbol": self.cfg["symbol"],
            "type": kind,
            "volume": volume,
            "price": price,
            "sl": signal["sl"],
            "tp": tp,
            "magic": MAGIC,
            "comment": "gc2:" + signal["signal_id"][:20],
            "deviation": self.cfg.get("deviation_points", 20),
            "type_time": self.mt.ORDER_TIME_GTC,
            "type_filling": self.cfg.get("filling_mode", 1),
        }
        if pending:
            request["type"] = {
                (True, "LIMIT"): self.mt.ORDER_TYPE_BUY_LIMIT,
                (False, "LIMIT"): self.mt.ORDER_TYPE_SELL_LIMIT,
                (True, "STOP"): self.mt.ORDER_TYPE_BUY_STOP,
                (False, "STOP"): self.mt.ORDER_TYPE_SELL_STOP,
            }[(buy, signal["order_type"])]
            request["type_filling"] = self.mt.ORDER_FILLING_RETURN
            request["type_time"] = self.mt.ORDER_TIME_SPECIFIED
            request["expiration"] = int(
                time.time() + self.cfg.get("pending_ttl_seconds", 3600)
            )
        self.preflight(request)
        return request

    def protection(self, risk, a):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        b = self.baseline
        if b.get("day") != day:
            # Reconstruct start-of-day equity from balance and realized P&L; excludes deposits/withdrawals.
            deals = self.mt.history_deals_get(
                datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
                datetime.now(timezone.utc),
            )
            if deals is None:
                raise ValueError("Cannot reconstruct daily risk baseline")
            trading = [
                d
                for d in deals
                if d.type in (self.mt.DEAL_TYPE_BUY, self.mt.DEAL_TYPE_SELL)
            ]
            pnl = sum(d.profit + d.commission + d.swap + d.fee for d in trading)
            b.update(
                day=day,
                daily_equity=max(a.balance - pnl, a.equity),
                high=max(b.get("high", 0), a.equity),
            )
        b["high"] = max(b.get("high", a.equity), a.equity)
        if a.equity <= b["daily_equity"] * (1 - risk["daily_loss_pct"] / 100):
            raise ValueError("Daily equity loss stop reached")
        if a.equity <= b["high"] * (1 - risk["drawdown_pct"] / 100):
            raise ValueError("Equity drawdown stop reached")

    def preflight(self, request):
        result = self.mt.order_check(request)
        if result is None or result.retcode != 0:
            raise ValueError("Broker preflight rejected request")

    def submit(self, request):
        result = self.mt.order_send(request)
        accepted = {self.mt.TRADE_RETCODE_DONE}
        if request["action"] == self.mt.TRADE_ACTION_PENDING:
            accepted.add(self.mt.TRADE_RETCODE_PLACED)
        if result is None or result.retcode not in accepted:
            return {
                "state": "uncertain",
                "detail": "Unconfirmed broker result: "
                + str(getattr(result, "retcode", "none")),
            }
        return {
            "state": "executed",
            "detail": "Broker confirmed request",
            "ticket": int(result.order),
        }

    def positions_for(self, signal_id):
        positions = self.mt.positions_get(symbol=self.cfg["symbol"])
        if positions is None:
            raise ValueError("Cannot inspect broker positions")
        comment = "gc2:" + signal_id[:20]
        matched = [p for p in positions if p.magic == MAGIC and p.comment == comment]
        # Reconcile deal -> position identifier when brokers modify position comments.
        if not matched:
            orders = self.mt.history_orders_get(
                datetime.fromtimestamp(time.time() - 30 * 86400, timezone.utc),
                datetime.now(timezone.utc),
            )
            if orders is None:
                raise ValueError("Cannot reconcile position history")
            identifiers = {
                o.position_id
                for o in orders
                if o.magic == MAGIC and o.comment == comment and o.position_id
            }
            matched = [
                p for p in positions if p.magic == MAGIC and p.identifier in identifiers
            ]
        return matched

    def prepare_control(self, command):
        self.check_account()
        self.quote()
        requests = []
        positions = self.mt.positions_get(symbol=self.cfg["symbol"])
        orders = self.mt.orders_get(symbol=self.cfg["symbol"])
        if positions is None or orders is None:
            raise ValueError("Cannot inspect broker orders")
        if command.get("signal_id"):
            matched = self.positions_for(command["signal_id"])
            comment = "gc2:" + command["signal_id"][:20]
            pending = [o for o in orders if o.magic == MAGIC and o.comment == comment]
        else:
            matched = [
                p
                for p in positions
                if p.magic == MAGIC and p.ticket == command.get("position")
            ]
            pending = [
                o
                for o in orders
                if o.magic == MAGIC and o.ticket == command.get("position")
            ]
        if command["action"] == "cancel":
            requests = [
                {
                    "action": self.mt.TRADE_ACTION_REMOVE,
                    "order": o.ticket,
                    "symbol": o.symbol,
                }
                for o in pending
            ]
        elif command["action"] in ("close", "partial"):
            for p in matched:
                percent = (
                    command.get("percent", 100)
                    if command["action"] == "partial"
                    else 100
                )
                req = self.close_request(p, percent)
                if req:
                    requests.append(req)
        elif command["action"] in ("modify", "breakeven"):
            for p in matched:
                override = command.get("overrides", {}).get(str(p.ticket), {})
                sl = (
                    p.price_open
                    if command["action"] == "breakeven"
                    else command.get("sl", p.sl)
                )
                tp = command.get("tp", p.tp)
                if override.get("sl"):
                    sl = p.sl
                if override.get("tp"):
                    tp = p.tp
                if sl == p.sl and tp == p.tp:
                    continue
                if command["action"] == "breakeven" and (
                    (p.type == 0 and p.sl >= sl) or (p.type == 1 and 0 < p.sl <= sl)
                ):
                    continue
                requests.append(
                    {
                        "action": self.mt.TRADE_ACTION_SLTP,
                        "position": p.ticket,
                        "symbol": p.symbol,
                        "sl": sl,
                        "tp": tp,
                        "magic": MAGIC,
                    }
                )
            if command["action"] == "modify":
                requests += [
                    {
                        "action": self.mt.TRADE_ACTION_MODIFY,
                        "order": o.ticket,
                        "symbol": o.symbol,
                        "price": o.price_open,
                        "sl": command.get("sl", o.sl),
                        "tp": command.get("tp", o.tp),
                        "type_time": o.type_time,
                        "expiration": o.time_expiration,
                    }
                    for o in pending
                ]
        if not requests:
            raise ValueError("No matching copier exposure needs this action")
        for request in requests:
            self.preflight(request)
        return requests

    def close_request(self, p, percent=None, volume=None):
        info, tick = self.quote()
        requested = volume if volume is not None else p.volume * percent / 100
        lots = floor_lots(min(requested, p.volume), info.volume_step)
        if lots < info.volume_min:
            return None
        remainder = p.volume - lots
        if remainder > 1e-8 and remainder < info.volume_min - 1e-8:
            raise ValueError("Partial close would leave less than broker minimum")
        return {
            "action": self.mt.TRADE_ACTION_DEAL,
            "position": p.ticket,
            "symbol": p.symbol,
            "volume": lots,
            "type": self.mt.ORDER_TYPE_SELL
            if p.type == self.mt.ORDER_TYPE_BUY
            else self.mt.ORDER_TYPE_BUY,
            "price": tick.bid if p.type == self.mt.ORDER_TYPE_BUY else tick.ask,
            "deviation": self.cfg.get("deviation_points", 20),
            "magic": MAGIC,
            "comment": "gc2:exit",
            "type_time": self.mt.ORDER_TIME_GTC,
            "type_filling": self.cfg.get("filling_mode", 1),
        }

    def snapshot(self):
        a = self.check_account()
        self.quote()
        positions = self.mt.positions_get()
        orders = self.mt.orders_get()
        if positions is None or orders is None:
            raise ValueError("Cannot read account exposure")
        now = datetime.now(timezone.utc)
        deals = self.mt.history_deals_get(
            now.replace(hour=0, minute=0, second=0, microsecond=0), now
        )
        if deals is None:
            raise ValueError("Cannot read trading history")
        return {
            "login": str(a.login),
            "server": a.server,
            "mode": self.cfg["mode"],
            "balance": a.balance,
            "equity": a.equity,
            "currency": a.currency,
            "daily_pnl": sum(
                d.profit + d.commission + d.swap + d.fee
                for d in deals
                if d.type in (self.mt.DEAL_TYPE_BUY, self.mt.DEAL_TYPE_SELL)
            ),
            "deals": [
                {
                    "ticket": d.ticket,
                    "position_id": d.position_id,
                    "symbol": d.symbol,
                    "side": "BUY" if d.type == 0 else "SELL",
                    "volume": d.volume,
                    "price": d.price,
                    "profit": d.profit,
                    "commission": d.commission,
                    "swap": d.swap,
                    "fee": d.fee,
                    "time": d.time,
                }
                for d in list(deals)[-100:]
                if d.type in (self.mt.DEAL_TYPE_BUY, self.mt.DEAL_TYPE_SELL)
                and d.entry in (self.mt.DEAL_ENTRY_OUT, self.mt.DEAL_ENTRY_OUT_BY)
            ],
            "positions": [
                {
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "side": "BUY" if p.type == 0 else "SELL",
                    "volume": p.volume,
                    "price_open": p.price_open,
                    "sl": p.sl,
                    "tp": p.tp,
                    "profit": p.profit,
                    "copier": p.magic == MAGIC,
                }
                for p in positions
            ],
            "orders": [
                {
                    "ticket": o.ticket,
                    "symbol": o.symbol,
                    "type": str(o.type),
                    "volume": o.volume_current,
                    "price_open": o.price_open,
                    "sl": o.sl,
                    "tp": o.tp,
                    "copier": o.magic == MAGIC,
                }
                for o in orders
            ],
        }
