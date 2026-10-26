from decimal import Decimal, ROUND_DOWN
import math


def size_volume(equity, risk_pct, loss_per_lot, step, minimum, maximum, cap):
    if not all(math.isfinite(x) and x > 0 for x in
               (equity, risk_pct, loss_per_lot, step, minimum, maximum, cap)):
        raise ValueError('Invalid sizing inputs')
    if risk_pct > 1:
        raise ValueError('Demo risk limit is 1% per trade')
    budget = equity * risk_pct / 100
    raw = min(budget / loss_per_lot, maximum, cap)
    volume = float((Decimal(str(raw)) / Decimal(str(step))).to_integral_value(
        rounding=ROUND_DOWN) * Decimal(str(step)))
    if volume < minimum:
        raise ValueError('Minimum lot exceeds risk budget; trade skipped')
    return volume


def validate_price(signal, bid, ask, max_spread, max_distance):
    numbers = (bid, ask, max_spread, max_distance, *[signal[k] for k in ('entry', 'sl', 'tp', 'be')])
    if not all(math.isfinite(x) and x > 0 for x in numbers):
        raise ValueError('Invalid price or guard configuration')
    if ask < bid or ask - bid > max_spread:
        raise ValueError('Spread exceeds limit')
    price = ask if signal['side'] == 'BUY' else bid
    if abs(price - signal['entry']) > max_distance:
        raise ValueError('Price moved too far from entry')
    if signal['side'] == 'BUY':
        valid = signal['sl'] < price < signal['be'] < signal['tp']
    else:
        valid = signal['tp'] < signal['be'] < price < signal['sl']
    if not valid:
        raise ValueError('Entry/SL/BE/TP invalid at current price')
    return price
