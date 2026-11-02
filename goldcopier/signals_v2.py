"""Deterministic signals: standard fields plus the supported trader alert templates."""

import math
import re

NUM = r"\d+(?:\.\d{1,10})?"
SYM = r"[A-Z0-9][A-Z0-9._-]{0,31}"
REF = r"[A-Z0-9_-]{1,100}"


def normalize(text):
    text = text.replace("**", "").replace("`", "").replace("•", "\n")
    text = re.sub(r"<@!?&?\d+>|@EVERYONE|@FOREX ALERT", "", text, flags=re.I)
    # Only a signed pip annotation is decoration; arbitrary prose remains invalid.
    text = re.sub(r"\s*\([+-]?\d+(?:\.\d+)?\s*PIPS?\)", "", text, flags=re.I)
    text = re.sub(
        rf"({SYM} (?:STOP LOSS HIT|TP [12] HIT))\s+[+-]{NUM} PIPS?\s*$",
        r"\1",
        text.upper(),
        flags=re.M,
    )
    return [
        re.sub(r"\s+", " ", line).strip().upper()
        for line in text.splitlines()
        if line.strip()
    ]


def price(value):
    n = float(value)
    if not math.isfinite(n) or not 0 < n < 1e12:
        raise ValueError("Invalid price")
    return n


def parse_signal(text):
    lines = [l for l in normalize(text) if l]
    if not lines:
        raise ValueError("Empty signal")
    reference = None
    for line in list(lines):
        m = re.fullmatch(rf"SIGNAL\s*:\s*({REF})", line)
        if m:
            if reference:
                raise ValueError("Duplicate signal reference")
            reference = m[1].lower()
            lines.remove(line)
    if not lines:
        raise ValueError("Missing instruction")
    control = re.fullmatch(rf"(CANCEL|CLOSE|BE)\s+SIGNAL:\s*({REF})", lines[0])
    if control and len(lines) == 1:
        return {
            "action": {"CANCEL": "cancel", "CLOSE": "close", "BE": "breakeven"}[
                control[1]
            ],
            "signal_id": control[2].lower(),
        }
    partial = re.fullmatch(rf"PARTIAL\s+SIGNAL:\s*({REF})", lines[0])
    if partial:
        m = re.fullmatch(rf"PERCENT:\s*({NUM})", lines[1]) if len(lines) == 2 else None
        if not m or not 0 < float(m[1]) <= 100:
            raise ValueError("PARTIAL requires a valid PERCENT")
        return {
            "action": "partial",
            "signal_id": partial[1].lower(),
            "percent": float(m[1]),
        }
    update = re.fullmatch(rf"UPDATE\s+SIGNAL:\s*({REF})", lines[0])
    alert = re.fullmatch(
        rf"({SYM})\s+(SET TP\s*:|MOVE SL TO BREAKEVEN(?:\s*\(({NUM})\))?|ENTRY(?: [12])? TRIGGERED|STOP LOSS HIT|TP [12] HIT|TRADE CLOSED(?:\s*\(({NUM})\))?)",
        lines[0],
    )
    if alert:
        symbol, event = alert[1], alert[2]
        base = {"symbol": symbol, **({"signal_id": reference} if reference else {})}
        if event.startswith("MOVE SL") and len(lines) == 1:
            return {
                "action": "breakeven",
                **base,
            }  # Actual follower fill, never the printed master entry.
        if not event.startswith("SET TP"):
            if len(lines) > 1 and not (
                len(lines) == 2
                and re.fullmatch(r"RESULT:\s*[+-]?\d+(?:\.\d+)?\s*PIPS?", lines[1])
            ):
                raise ValueError("Unexpected status text")
            return {
                "action": "notice",
                "notice": event,
                "closed": event.startswith(("STOP LOSS", "TRADE CLOSED")),
                **base,
            }
        lines = ["UPDATE", *lines[1:]]
        update = True
    else:
        base = {"signal_id": update[1].lower()} if update else {}
    # Trader variant: BUY SYMBOL CMP price, or PLACE SYMBOL BUY LIMIT.
    cmp_head = re.fullmatch(
        rf"(BUY|SELL)\s+({SYM})\s+CMP(?:\s*:?[ ]*({NUM}))?", lines[0]
    )
    if cmp_head:
        lines = [
            f"{cmp_head[2]} {cmp_head[1]} CMP",
            f"ENTRY: {cmp_head[3] or 'CMP'}",
            *lines[1:],
        ]
    else:
        lines[0] = re.sub(
            rf"^PLACE\s+({SYM})\s+(BUY|SELL)\s+(LIMIT|STOP)$", r"\1 \2 \3", lines[0]
        )
    head = re.fullmatch(
        rf"({SYM})\s+(BUY|SELL)(?:\s+(MARKET|CMP|AUTO|LIMIT|STOP))?", lines[0]
    )
    if not head and not update:
        raise ValueError(
            "Use SYMBOL BUY/SELL [CMP/MARKET/AUTO/LIMIT/STOP] or a referenced update"
        )
    values = {}
    factor = 1.0
    for line in lines[1:]:
        if re.fullmatch(r"ENTRY\s*:\s*CMP", line):
            if "ENTRY" in values or update:
                raise ValueError("Invalid or duplicate ENTRY")
            values["ENTRY"] = None
            continue
        if line == "HALF RISK (0.5R)":
            if factor != 1 or update:
                raise ValueError("Invalid risk instruction")
            factor = 0.5
            continue
        m = re.fullmatch(
            rf"(ENTRY|SL|TP|TP1|TP2|BE)\s*:\s*({NUM})(?:\s*([-|])\s*({NUM}))?", line
        )
        if not m or m[1] in values or (m[4] and m[1] != "ENTRY"):
            raise ValueError("Invalid or duplicate field")
        values[m[1]] = [price(m[2]), price(m[4])] if m[4] else price(m[2])
        if m[3] == "|":
            values["LEGS"] = True
    if "TP" in values and ("TP1" in values or "TP2" in values):
        raise ValueError("Use TP or TP1/TP2, not both")
    if "TP2" in values and "TP1" not in values:
        raise ValueError("TP2 requires TP1")
    targets = (
        [values["TP"]]
        if "TP" in values
        else [values[k] for k in ("TP1", "TP2") if k in values]
    )
    if update:
        if not values or set(values) - {"SL", "TP", "TP1", "TP2"}:
            raise ValueError("UPDATE supports SL and TP/TP1/TP2 only")
        return {
            "action": "modify",
            **base,
            **({"sl": values["SL"]} if "SL" in values else {}),
            **(
                {"targets": targets, "tp": targets[-1], "multi_target": "TP1" in values}
                if targets
                else {}
            ),
        }
    if head[3] in ("CMP", "MARKET") and "ENTRY" not in values:
        values["ENTRY"] = None
    if not {"ENTRY", "SL"} <= set(values):
        raise ValueError("ENTRY (or CMP price) and SL required")
    entry = values["ENTRY"]
    low, high = sorted(entry) if isinstance(entry, list) else (entry, entry)
    if isinstance(entry, list) and low == high:
        raise ValueError("Entry prices must differ")
    kind = head[3] or "AUTO"
    if entry is None:
        if kind in ("LIMIT", "STOP"):
            raise ValueError("Pending orders require an entry price")
        kind = "CMP"

    if kind in ("LIMIT", "STOP") and isinstance(entry, list) and not values.get("LEGS"):
        raise ValueError(
            "Pending orders require exact entries; use | for separate legs"
        )
    if values.get("LEGS") and kind not in ("LIMIT", "STOP"):
        raise ValueError("Separate legs require explicit LIMIT or STOP")
    sl, be = values["SL"], values.get("BE")
    buy = head[2] == "BUY"
    if entry is not None and ((buy and sl >= low) or (not buy and sl <= high)):
        raise ValueError("Invalid SL/entry order")
    edge = (high if buy else low) if entry is not None else sl
    if targets and ((buy and targets[0] <= edge) or (not buy and targets[0] >= edge)):
        raise ValueError("Invalid target order")
    if be and (
        (buy and (be <= edge or (targets and be > targets[0])))
        or (not buy and (be >= edge or (targets and be < targets[0])))
    ):
        raise ValueError("Invalid BE order")
    if len(targets) > 1 and (
        (buy and targets[1] <= targets[0]) or (not buy and targets[1] >= targets[0])
    ):
        raise ValueError("Targets must progress in the trade direction")
    return {
        "action": "entry",
        "symbol": head[1],
        "side": head[2],
        "order_type": "MARKET" if kind == "CMP" else kind,
        "execution_mode": kind,
        "entry": (low + high) / 2 if entry is not None else None,
        "entry_low": low,
        "entry_high": high,
        "sl": sl,
        "be": be,
        "targets": targets,
        "tp": targets[-1] if targets else 0,
        "multi_target": "TP1" in values,
        "risk_factor": factor,
        **({"entry_legs": entry} if values.get("LEGS") else {}),
    }


def parse_signals(text):
    pieces = re.split(
        r"\n\s*---+\s*\n|\n(?=\s*(?:(?:PLACE\s+)?[A-Za-z0-9][A-Za-z0-9._-]{0,31}\s+(?:BUY|SELL)\b|(?:BUY|SELL)\s+[A-Za-z0-9._-]+\s+CMP\b))",
        text.strip(),
        flags=re.I,
    )
    signals = []
    for piece in pieces:
        if not piece.strip():
            continue
        item = parse_signal(piece)
        legs = item.pop("entry_legs", None)
        if legs:
            signals.extend(
                {
                    **item,
                    "entry": leg,
                    "entry_low": leg,
                    "entry_high": leg,
                    "risk_factor": item["risk_factor"] / len(legs),
                }
                for leg in legs
            )
        else:
            signals.append(item)
    if not signals:
        raise ValueError("Empty signal")
    if len(signals) > 20:
        raise ValueError("At most 20 signals per message")
    return signals
