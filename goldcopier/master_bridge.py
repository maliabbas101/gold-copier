"""Optional read-only MT5 analyst bridge: positions -> standard signal events.

Only positions opened after initialization are mirrored. Existing positions are
baselined, never backfilled. Pending master orders are not mirrored by this bridge.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from urllib.parse import urlparse

import httpx


def changes(before, after):
    """Yield idempotent commands, referenced to an already mirrored master position."""
    reference = before["reference"]
    events = []
    if after is None:
        return [("close", f"CLOSE SIGNAL: {reference}")]
    if before["sl"] != after["sl"] or before["tp"] != after["tp"]:
        if not after["sl"] or not after["tp"]:
            raise ValueError("Removing master SL/TP requires operator review")
        events.append(
            (
                "modify",
                f"UPDATE SIGNAL: {reference}\nSL: {after['sl']}\nTP: {after['tp']}",
            )
        )
    if after["volume"] < before["volume"]:
        percent = round(
            (before["volume"] - after["volume"]) / before["volume"] * 100, 3
        )
        if percent > 0:
            events.append(
                ("partial", f"PARTIAL SIGNAL: {reference}\nPERCENT: {percent}")
            )
    elif after["volume"] > before["volume"]:
        raise ValueError("Master scale-in requires a new explicit signal")
    return events


def main():
    import MetaTrader5 as mt

    logging.basicConfig(level=logging.INFO)
    url = os.environ["COPIER_URL"].rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    ):
        raise ValueError("HTTPS required")
    if not mt.initialize(path=os.environ["MASTER_TERMINAL_PATH"]):
        raise ValueError("Master terminal unavailable")
    expected_login = os.environ["MASTER_LOGIN"]
    expected_server = os.environ["MASTER_SERVER"]
    symbol = os.environ.get("MASTER_SYMBOL", "XAUUSD")
    statefile = Path(os.environ.get("MASTER_STATE", "master-state.json"))
    state = json.loads(statefile.read_text()) if statefile.exists() else None
    with httpx.Client(
        base_url=url,
        headers={"Authorization": "Bearer " + os.environ["SIGNAL_INGEST_TOKEN"]},
        timeout=10,
    ) as client:
        while True:
            try:
                account = mt.account_info()
                if (
                    account is None
                    or str(account.login) != expected_login
                    or account.server != expected_server
                ):
                    raise ValueError("Master account identity changed")
                positions = mt.positions_get(symbol=symbol)
                if positions is None:
                    raise ValueError("Master positions unavailable")
                now = time.time()
                current = {
                    str(p.identifier): {
                        "ticket": p.ticket,
                        "side": "BUY" if p.type == 0 else "SELL",
                        "entry": p.price_open,
                        "sl": p.sl,
                        "tp": p.tp,
                        "volume": p.volume,
                        "time": p.time,
                        "version": p.time_update_msc,
                        "reference": "master-" + str(p.identifier) + "-entry",
                    }
                    for p in positions
                }
                if state is None:
                    state = {k: {**v, "mirrored": False} for k, v in current.items()}
                else:
                    for ident, p in current.items():
                        previous = state.get(ident)
                        if previous is None:
                            if now - p["time"] > 60 or not p["sl"] or not p["tp"]:
                                current[ident]["mirrored"] = False
                                continue
                            direction = 1 if p["side"] == "BUY" else -1
                            be = p["entry"] + direction * min(
                                abs(p["entry"] - p["sl"]), abs(p["tp"] - p["entry"]) / 2
                            )
                            text = f"XAUUSD {p['side']}\nENTRY: {p['entry']:.3f}\nSL: {p['sl']:.3f}\nTP: {p['tp']:.3f}\nBE: {be:.3f}"
                            r = client.post(
                                "/api/ingest",
                                json={
                                    "analyst_id": os.environ["MIRROR_ANALYST_ID"],
                                    "source_id": p["reference"],
                                    "text": text,
                                    "created": p["time"],
                                },
                            )
                            r.raise_for_status()
                            current[ident]["mirrored"] = True
                        else:
                            current[ident]["mirrored"] = previous.get("mirrored", False)
                            if previous.get("mirrored"):
                                for action, text in changes(previous, p):
                                    source = f"master-{ident}-{action}-{p['version']}"
                                    r = client.post(
                                        "/api/ingest",
                                        json={
                                            "analyst_id": os.environ[
                                                "MIRROR_ANALYST_ID"
                                            ],
                                            "source_id": source[:100],
                                            "text": text,
                                            "created": now,
                                        },
                                    )
                                    r.raise_for_status()
                    for ident, previous in state.items():
                        if ident not in current and previous.get("mirrored"):
                            r = client.post(
                                "/api/ingest",
                                json={
                                    "analyst_id": os.environ["MIRROR_ANALYST_ID"],
                                    "source_id": f"master-{ident}-close",
                                    "text": f"CLOSE SIGNAL: {previous['reference']}",
                                    "created": now,
                                },
                            )
                            r.raise_for_status()
                    state = current
                temp = statefile.with_suffix(".tmp")
                temp.write_text(json.dumps(state))
                temp.replace(statefile)
            except (httpx.HTTPError, ValueError):
                logging.exception(
                    "Master synchronization failed; state retained for idempotent retry"
                )
            time.sleep(1)


if __name__ == "__main__":
    main()
