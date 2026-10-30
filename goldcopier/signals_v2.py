"""Deterministic XAUUSD signal/management grammar; no guessing financial instructions."""

import re
import math


def parse_signal(text):
    lines = [line.strip().upper() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise ValueError("Empty signal")
    control = re.fullmatch(
        r"(CANCEL|CLOSE|BE)\s+SIGNAL:\s*([A-Z0-9_-]{1,100})", lines[0]
    )
    if control and len(lines) == 1:
        return {
            "action": {"CANCEL": "cancel", "CLOSE": "close", "BE": "breakeven"}[
                control[1]
            ],
            "signal_id": control[2].lower(),
        }
    partial = re.fullmatch(r"PARTIAL\s+SIGNAL:\s*([A-Z0-9_-]{1,100})", lines[0])
    if partial:
        if len(lines) != 2:
            raise ValueError("PARTIAL requires PERCENT")
        m = re.fullmatch(r"PERCENT:\s*(\d+(?:\.\d{1,3})?)", lines[1])
        if not m or not 0 < float(m[1]) <= 100:
            raise ValueError("Invalid partial percentage")
        return {
            "action": "partial",
            "signal_id": partial[1].lower(),
            "percent": float(m[1]),
        }
    update = re.fullmatch(r"UPDATE\s+SIGNAL:\s*([A-Z0-9_-]{1,100})", lines[0])
    head = re.fullmatch(r"XAUUSD\s+(BUY|SELL)(?:\s+(MARKET|LIMIT|STOP))?", lines[0])
    if not head and not update:
        raise ValueError(
            "Use XAUUSD BUY/SELL [MARKET/LIMIT/STOP] or a referenced management command"
        )
    values = {}
    for line in lines[1:]:
        m = re.fullmatch(
            r"(ENTRY|SL|TP|TP1|TP2|BE):\s*(\d+(?:\.\d{1,3})?)(?:\s*-\s*(\d+(?:\.\d{1,3})?))?",
            line,
        )
        if not m or m[1] in values or (m[3] and m[1] != "ENTRY"):
            raise ValueError("Invalid or duplicate field")
        nums = [float(x) for x in (m[2], m[3]) if x]
        if any(not math.isfinite(x) or not 0 < x < 1_000_000 for x in nums):
            raise ValueError("Invalid price")
        values[m[1]] = nums if len(nums) == 2 else nums[0]
    if update:
        if not values or set(values) - {"SL", "TP"}:
            raise ValueError("UPDATE supports SL and TP only")
        return {
            "action": "modify",
            "signal_id": update[1].lower(),
            **{k.lower(): v for k, v in values.items()},
        }
    if not {"ENTRY", "SL", "BE"} <= set(values) or not ({"TP", "TP1"} & set(values)):
        raise ValueError("ENTRY, SL, BE and TP or TP1 required")
    if "TP" in values and ("TP1" in values or "TP2" in values):
        raise ValueError("Use TP or TP1/TP2, not both")
    if "TP2" in values and "TP1" not in values:
        raise ValueError("TP2 requires TP1")
    entry = values["ENTRY"]
    low, high = sorted(entry) if isinstance(entry, list) else (entry, entry)
    if isinstance(entry, list) and low == high:
        raise ValueError("Entry range requires two different prices")
    kind = head[2] or "MARKET"
    if kind != "MARKET" and isinstance(entry, list):
        raise ValueError("Pending orders require an exact entry")
    targets = (
        [values["TP"]]
        if "TP" in values
        else [values["TP1"]] + ([values["TP2"]] if "TP2" in values else [])
    )
    sl, be = values["SL"], values["BE"]
    buy = head[1] == "BUY"
    if (buy and not sl < low <= high < be <= targets[0]) or (
        not buy and not targets[0] <= be < low <= high < sl
    ):
        raise ValueError("Invalid SL/entry/BE/target order")
    if len(targets) > 1 and (
        (buy and targets[1] <= targets[0]) or (not buy and targets[1] >= targets[0])
    ):
        raise ValueError("Targets must progress in the trade direction")
    return {
        "action": "entry",
        "symbol": "XAUUSD",
        "side": head[1],
        "order_type": kind,
        "entry": (low + high) / 2,
        "entry_low": low,
        "entry_high": high,
        "sl": sl,
        "be": be,
        "targets": targets,
        "tp": targets[-1],
        "multi_target": "TP1" in values,
    }
