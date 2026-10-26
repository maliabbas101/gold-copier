from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import re


@dataclass(frozen=True)
class Signal:
    side: str
    entry: float
    sl: float
    tp: float
    be: float

    def asdict(self):
        return asdict(self)


def parse(text: str) -> Signal:
    """Strict format: no fuzzy matching, price ranges, multiple TPs, or inferred fields."""
    lines = text.strip().upper().splitlines()
    if len(lines) != 5:
        raise ValueError('Expected five lines: XAUUSD BUY/SELL, ENTRY, SL, TP, BE')
    head = re.fullmatch(r'XAUUSD\s+(BUY|SELL)', lines[0].strip())
    if not head:
        raise ValueError('Only XAUUSD BUY or XAUUSD SELL is supported')
    values = {}
    for line in lines[1:]:
        match = re.fullmatch(r'(ENTRY|SL|TP|BE)\s*:\s*([0-9]+(?:\.[0-9]{1,3})?)', line.strip())
        if not match or match[1] in values:
            raise ValueError('Invalid or duplicate field')
        try:
            price = Decimal(match[2])
        except InvalidOperation as exc:
            raise ValueError('Invalid price') from exc
        if not 0 < price < 1_000_000:
            raise ValueError('Price out of bounds')
        values[match[1]] = float(price)
    if set(values) != {'ENTRY', 'SL', 'TP', 'BE'}:
        raise ValueError('Missing fields')
    e, sl, tp, be = (values[k] for k in ('ENTRY', 'SL', 'TP', 'BE'))
    if head[1] == 'BUY' and not sl < e < be < tp:
        raise ValueError('BUY requires SL < ENTRY < BE < TP')
    if head[1] == 'SELL' and not tp < be < e < sl:
        raise ValueError('SELL requires TP < BE < ENTRY < SL')
    return Signal(head[1], e, sl, tp, be)
