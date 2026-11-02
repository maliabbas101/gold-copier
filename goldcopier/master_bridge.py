"""Legacy change-format helper; live observation uses the scoped master source worker."""

import os


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
    """Compatibility entry point for the account-scoped read-only source worker."""
    if not os.environ.get("AGENT_TOKEN"):
        raise ValueError(
            "Configure a trader source in Operator console, then use its scoped AGENT_TOKEN with goldcopier.platform_agent. The legacy global-token bridge is retired."
        )
    from .platform_agent import main as run_worker

    return run_worker()


if __name__ == "__main__":
    main()
