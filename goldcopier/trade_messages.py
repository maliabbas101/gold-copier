"""Plain-language trade updates with stable human references and optional diagnostics."""

import json
import os
from decimal import Decimal, InvalidOperation
import re


def number(value):
    return format(float(value), ".10f").rstrip("0").rstrip(".")


def context(db, feed_id, payload):
    original_id = (
        feed_id if payload["action"] == "entry" else payload.get("signal_id", feed_id)
    )
    row = db.execute("SELECT payload FROM feeds WHERE id=?", (original_id,)).fetchone()
    original = json.loads(row[0]) if row else payload
    label = f"{original.get('symbol', payload.get('symbol', 'Trade'))} {original.get('side', '')}".strip()
    return f"{label} · Trade #{original_id[:8].upper()}"


def configured_roles(db, analyst_id):
    row = db.execute(
        "SELECT discord_role_ids FROM communities WHERE analyst_id=?", (analyst_id,)
    ).fetchone()
    return json.loads(row[0]) if row else []


def pip_size(symbol):
    # These are display units, independent of broker quote precision and risk sizing.
    try:
        overrides = json.loads(os.environ.get("DISCORD_PIP_SIZES") or "{}")
        if not isinstance(overrides, dict):
            overrides = {}
    except (ValueError, TypeError):
        overrides = {}
    currencies = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
    default = (
        0.1
        if symbol == "XAUUSD"
        else (0.01 if symbol.endswith("JPY") else 0.0001)
        if len(symbol) == 6 and symbol[:3] in currencies and symbol[3:] in currencies
        else None
    )
    value = overrides.get(symbol, default)
    if value is None:
        return None
    try:
        unit = Decimal(str(value))
        return unit if unit.is_finite() and unit > 0 else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def confirmation(db, feed):
    p = json.loads(feed["payload"])
    original = db.execute(
        "SELECT payload FROM feeds WHERE id=?", (p.get("signal_id", feed["id"]),)
    ).fetchone()
    original = json.loads(original[0]) if original else p
    symbol = original.get("symbol", p.get("symbol", ""))
    side = original.get("side", p.get("side", "BUY"))
    basis = (
        original.get("master_entry") or p.get("master_entry") or original.get("entry")
    )
    unit = pip_size(symbol)

    def distance(value):
        if basis is None or unit is None:
            return "— pips"
        signed = (
            (Decimal(str(value)) - Decimal(str(basis)))
            * (1 if side == "BUY" else -1)
            / unit
        )
        prefix = "+" if signed > 0 else ""
        return f"{prefix}{number(signed)} pips"

    roles = configured_roles(db, feed["analyst_id"])
    mentions = (
        ([f"**<@&{roles[0]}>**"] + [f"<@&{role}>" for role in roles[1:]])
        if roles
        else []
    )
    lines = ([" ".join(mentions)] if mentions else []) + [context(db, feed["id"], p)]
    action = p["action"]
    if action == "entry":
        lines.append(
            "Entry: CMP" if p.get("entry") is None else f"Entry: {number(p['entry'])}"
        )
    else:
        lines.append(
            {
                "modify": "SL / TP updated",
                "breakeven": "SL moved to breakeven",
                "close": "Trade closed",
                "partial": "Trade partially closed",
                "cancel": "Pending order cancelled",
            }.get(action, "Trade updated")
        )
    if "sl" in p:
        lines.append(f"SL: {number(p['sl'])} ({distance(p['sl'])})")
    if "tp" in p or "targets" in p:
        targets = p.get("targets") or ([p["tp"]] if p.get("tp") else [])
        if not targets:
            lines.append("TP: not set yet (— pips)")
        for i, target in enumerate(targets):
            label = f"TP{i + 1}" if len(targets) > 1 else "TP"
            lines.append(f"{label}: {number(target)} ({distance(target)})")
    if action == "partial":
        lines.append(f"Close: {number(p.get('percent', 100))}%")
    return "\n".join(lines)


def failure_help(detail):
    """Never infer a specific broker failure when the provider returned no reason."""
    lower = detail.lower()
    code = re.search(r"(?:code=|MT5 error )(-?\d+)", detail)
    code = int(code[1]) if code else None
    mapping = {
        -2: (
            "MT5 could not read the trade request.",
            "Update and restart the follower bot. If it happens again, share the technical details with support.",
        ),
        -8: (
            "Automatic trading is disabled in MT5.",
            "Enable Algo Trading and allow external Python trading in the follower terminal.",
        ),
        -10004: (
            "The bot could not connect to the follower MT5 terminal.",
            "Open the follower terminal and check that it is logged in and connected.",
        ),
        -10005: (
            "The follower terminal took too long to respond.",
            "Check its connection and terminal journal before placing a new test trade.",
        ),
        10016: (
            "The stop loss or take profit is not allowed at the current follower price.",
            "Check the broker minimum stop distance and current market price.",
        ),
        10019: (
            "The follower account does not have enough free margin.",
            "Check the account balance, open trades and lot size.",
        ),
        10018: (
            "The market is closed on the follower broker.",
            "Wait until this instrument is open for trading.",
        ),
        10030: (
            "The follower broker does not accept this order filling method.",
            "Check the broker symbol settings with your service administrator.",
        ),
        10027: (
            "Automatic trading is disabled in the follower terminal.",
            "Enable Algo Trading and allow external Python trading.",
        ),
        10017: (
            "Trading is disabled for the follower account.",
            "Check broker trading permissions and use the trading password, not an investor password.",
        ),
    }
    if code in mapping:
        return mapping[code]
    if "no matching copier" in lower or "original copied trade did not open" in lower:
        return (
            "There is no copied trade to update or close.",
            "The original copy did not open or has already closed. No action was sent to the broker.",
        )
    if "preflight" in lower and ("none" in lower or "no result" in lower):
        return (
            "MT5 did not return a result when checking the follower trade.",
            "Check the follower terminal connection. Open Technical details for the MT5 error before trying a new master trade.",
        )
    for needle, explanation, step in (
        (
            "spread",
            "The follower broker spread is above your allowed limit.",
            "Review the spread limit or wait for a smaller spread.",
        ),
        (
            "stop loss",
            "The stop loss is not valid at the follower market price.",
            "Check the stop loss and current follower price.",
        ),
        (
            "minimum broker lot",
            "The smallest trade allowed by the broker exceeds your risk budget.",
            "Review account size and risk settings.",
        ),
        (
            "risk cap",
            "The trade exceeds your configured risk limit.",
            "Review risk settings and existing exposure.",
        ),
        (
            "copying is paused",
            "Copying is paused for this follower.",
            "Resume copying when you are ready.",
        ),
        (
            "offline",
            "The follower bot is offline.",
            "Check the follower connection before placing a new master trade.",
        ),
        (
            "no enabled mapping",
            "This instrument is not configured for the follower.",
            "Open Trading pairs and map it to the follower broker symbol.",
        ),
    ):
        if needle in lower:
            return explanation, step
    return (
        "The follower could not process this instruction.",
        "Open Technical details or contact your service administrator before placing a new test trade.",
    )


def outcome(db, feed, result, job_id=""):
    p = json.loads(feed["payload"])
    label = context(db, feed["id"], p)
    if result["state"] == "executed":
        action = {
            "entry": "Trade copied successfully.",
            "modify": "Stop loss / take profit updated.",
            "close": "Copied trade closed.",
            "partial": "Copied trade partially closed.",
            "breakeven": "Stop loss moved to breakeven.",
            "cancel": "Pending copied order cancelled.",
        }.get(p["action"], "Trade instruction completed.")
        return f"{label}\n{action}"
    if result["state"] == "uncertain":
        summary = "Trade outcome needs review. The broker has not confirmed whether it executed. New entries are paused; check broker history before taking action."
    else:
        reason, step = failure_help(result["detail"])
        summary = (
            (
                "Trade was not copied. "
                if p["action"] == "entry"
                else "Trade update was not applied. "
            )
            + reason
            + "\nNext step: "
            + step
        )
    technical = result["detail"] + (f" | Job: {job_id}" if job_id else "")
    return f"{label}\n{summary}\nTechnical details: {technical}"
