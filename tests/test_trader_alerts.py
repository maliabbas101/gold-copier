import json
import time
from types import SimpleNamespace as NS

import pytest

from test_platform import workspace, add_account, worker_ready
from test_worker_v2 import connected_v2, BrokerMT
from goldcopier.signals_v2 import parse_signal, parse_signals
from goldcopier.platform_models import Risk
from goldcopier.platform_agent import Ledger, execute, monitor
from goldcopier.broker_v2 import MAGIC


@pytest.mark.parametrize(
    "text,kind,targets",
    [
        (
            "**@everyone** @Forex Alert BUY XAUUSD CMP 3900.7\nSL : 3894.7 (-60 pips)",
            "MARKET",
            [],
        ),
        (
            "PLACE XAUUSD SELL LIMIT\nEntry : 3909.0\nSL : 3920.0 (-110 pips)\nTP : 3870.5 (+385 pips)",
            "LIMIT",
            [3870.5],
        ),
        (
            "SELL XAUUSD CMP 3940.4\nHalf Risk (0.5r)\nSL : 3951.0 (-106 pips)",
            "MARKET",
            [],
        ),
    ],
)
def test_trader_entry_templates(text, kind, targets):
    signal = parse_signal(text)
    assert signal["order_type"] == kind
    assert signal["targets"] == targets
    assert signal["be"] is None
    if "Half Risk" in text:
        assert signal["risk_factor"] == 0.5


def test_explicit_legs_split_total_half_risk():
    items = parse_signals(
        "PLACE XAUUSD BUY LIMIT\nEntry : 3947.0 | 3943.5\nHalf Risk (0.5r)\nSL : 3934.5 (-125 pips)"
    )
    assert [x["entry"] for x in items] == [3947, 3943.5]
    assert [x["risk_factor"] for x in items] == [0.25, 0.25]


@pytest.mark.parametrize(
    "text",
    [
        "XAUUSD Stop Loss Hit -60 pips",
        "XAUUSD Entry Triggered",
        "XAUUSD Entry 1 Triggered",
        "XAUUSD TP 1 Hit (+80 pips)",
        "XAUUSD Trade Closed (3947.9)\nResult: -75 pips",
    ],
)
def test_status_announcements_never_place_trades(text):
    assert parse_signal(text)["action"] == "notice"


def test_tp_and_be_alerts():
    s = parse_signal(
        "@everyone @Forex Alert XAUUSD Set TP: • TP1: 3929.5 (+109 pips) • TP2: 3924.0 (+164 pips)\nSIGNAL: 1234"
    )
    assert s["targets"] == [3929.5, 3924]
    assert s["signal_id"] == "1234"
    s = parse_signal("XAUUSD Move SL to Breakeven (3947.0)")
    assert s["action"] == "breakeven"
    assert "sl" not in s  # Broker actual fill is used instead of master price.


def test_cmp_executes_with_sl_and_without_tp_or_be(connected_v2, tmp_path):
    b, mt = connected_v2
    s = parse_signal("BUY XAUUSD CMP 2700\nSL: 2695")
    s["signal_id"] = "a" * 32
    ledger = Ledger(tmp_path / "worker.db")
    execute(
        {
            "id": "cmp",
            "signal": s,
            "risk": Risk().model_dump(),
            "expires": time.time() + 60,
        },
        b,
        ledger,
    )
    assert mt.sent[0]["action"] == mt.TRADE_ACTION_DEAL
    assert mt.sent[0]["price"] == mt.tick.ask
    assert mt.sent[0]["tp"] == 0
    mt.positions = (
        NS(
            ticket=1234,
            symbol="XAUUSDm",
            magic=MAGIC,
            comment="gc2:" + "a" * 20,
            type=0,
            volume=0.09,
            price_open=2700.2,
            sl=2695,
            tp=0,
        ),
    )
    monitor(b, ledger)
    assert len(mt.sent) == 1


@pytest.mark.parametrize(
    "side,entry,expected",
    [("BUY", 2699, 2), ("BUY", 2701, 4), ("SELL", 2701, 3), ("SELL", 2699, 5)],
)
def test_exact_untyped_entries_place_waiting_broker_orders(
    connected_v2, side, entry, expected
):
    b, mt = connected_v2
    sl = 2690 if side == "BUY" else 2710
    s = parse_signal(f"XAUUSD {side}\nENTRY: {entry}\nSL: {sl}")
    s["signal_id"] = "b" * 32
    request = b.prepare_entry(s, Risk().model_dump(), "future")
    assert request["action"] == mt.TRADE_ACTION_PENDING
    assert request["type"] == expected
    assert request["price"] == entry


def test_cmp_is_not_turned_into_pending_when_price_moves(connected_v2):
    b, mt = connected_v2
    s = parse_signal("BUY XAUUSD CMP 2690\nSL: 2680")
    s["signal_id"] = "c" * 32
    with pytest.raises(ValueError, match="outside entry tolerance"):
        b.prepare_entry(s, Risk().model_dump(), "late")
    assert mt.sent == []


def test_multileg_updates_link_both_jobs_and_keep_total_risk(workspace):
    w = workspace
    a = add_account(w)
    worker_ready(w, a)

    def ingest(source, text, **kw):
        return w.store.ingest(w.analyst, source, text, time.time(), **kw)

    result = ingest(
        "100",
        "PLACE XAUUSD BUY LIMIT\nEntry: 3947 | 3943.5\nHalf Risk (0.5r)\nSL: 3934.5",
    )
    assert result["queued"] == 2
    jobs = w.store.dashboard(w.owner)["jobs"]
    with w.store.db() as db:
        risks = [
            json.loads(r[0])
            for r in db.execute("SELECT risk FROM pjobs WHERE account_id=?", (a,))
        ]
    assert sum(r["risk_pct"] for r in risks) == 0.25
    update = ingest("101", "XAUUSD Set TP:\nTP1: 3955\nTP2: 3975")
    assert update["queued"] == 2
    notice = ingest("102", "XAUUSD Entry 1 Triggered")
    assert notice["queued"] == 0
    ingest("103", "BUY XAUUSD CMP 3900.7\nSL: 3894.7")
    with pytest.raises(ValueError, match="Ambiguous"):
        ingest("104", "XAUUSD Move SL to Breakeven (3947)")
    assert (
        ingest("105", "XAUUSD Move SL to Breakeven (3947)", reference_id="100")[
            "queued"
        ]
        == 2
    )
    assert ingest("106", "XAUUSD Set TP:\nTP: 3960\nSIGNAL: 100-1")["queued"] == 1


def test_paused_or_stale_followers_have_visible_skip_reasons(workspace):
    w = workspace
    a = add_account(w)
    worker_ready(w, a)
    w.store.set_enabled(w.owner, a, False)
    r = w.store.ingest(w.analyst, "pause", "BUY XAUUSD CMP 2700\nSL: 2695", time.time())
    assert r["queued"] == 0 and "paused" in r["skipped"][0]["reason"]
    assert any(
        "signal skipped" in e["detail"] for e in w.store.dashboard(w.owner)["events"]
    )


def test_late_tp_updates_worker_journal_and_pending_order(connected_v2, tmp_path):
    b, mt = connected_v2
    ledger = Ledger(tmp_path / "worker.db")
    s = parse_signal("XAUUSD BUY LIMIT\nENTRY: 2699\nSL: 2695")
    s["signal_id"] = "d" * 32
    risk = Risk().model_dump()
    execute(
        {"id": "entry", "signal": s, "risk": risk, "expires": time.time() + 60},
        b,
        ledger,
    )
    mt.orders = (
        NS(
            ticket=1234,
            symbol="XAUUSDm",
            magic=MAGIC,
            comment="gc2:" + "d" * 20,
            price_open=2699,
            sl=2695,
            tp=0,
            type_time=2,
            time_expiration=int(time.time() + 3600),
        ),
    )
    u = {
        "action": "modify",
        "signal_id": "d" * 32,
        "targets": [2710, 2720],
        "tp": 2720,
        "multi_target": True,
    }
    execute(
        {"id": "tp", "signal": u, "risk": risk, "expires": time.time() + 60}, b, ledger
    )
    assert mt.sent[-1]["action"] == mt.TRADE_ACTION_MODIFY
    original = json.loads(
        ledger.db.execute("SELECT job FROM operations WHERE id='entry'").fetchone()[0]
    )
    assert original["signal"]["targets"] == [2710, 2720]
    assert original["signal"]["multi_target"]


def test_auto_entry_zone_waits_at_first_reached_edge(connected_v2):
    b, mt = connected_v2
    s = parse_signal("XAUUSD BUY\nENTRY: 2698 - 2699\nSL: 2690")
    s["signal_id"] = "e" * 32
    request = b.prepare_entry(s, Risk().model_dump(), "zone")
    assert request["type"] == mt.ORDER_TYPE_BUY_LIMIT
    assert request["price"] == 2699


def test_market_fill_policy_comes_from_broker_symbol(connected_v2):
    b, mt = connected_v2
    original = mt.symbol_info

    def info(symbol):
        i = original(symbol)
        i.filling_mode = 1
        i.trade_exemode = 2
        return i

    mt.symbol_info = info
    s = parse_signal("BUY XAUUSD CMP 2700\nSL: 2695")
    s["signal_id"] = "f" * 32
    assert b.prepare_entry(s, Risk().model_dump(), "fill")["type_filling"] == 0


def test_broker_rejection_and_pending_acceptance_have_clear_outcomes(connected_v2):
    b, mt = connected_v2
    mt.result = NS(retcode=10030, comment="Invalid fill", order=0)
    r = b.submit({"action": mt.TRADE_ACTION_DEAL})
    assert r["state"] == "rejected" and "Invalid fill" in r["detail"]
    mt.result = NS(retcode=10008, order=42)
    r = b.submit({"action": mt.TRADE_ACTION_PENDING})
    assert r["state"] == "executed" and "waiting for price trigger" in r["detail"]


def test_discord_embed_and_reply_bind_to_authorized_original(workspace):
    from datetime import datetime, timezone
    from goldcopier.discord_platform import ingest

    w = workspace
    a = add_account(w)
    worker_ready(w, a)

    def message(id, description, ref=None):
        return NS(
            id=id,
            guild=NS(id=1),
            channel=NS(id=2),
            author=NS(id=3, bot=True),
            webhook_id=None,
            content="",
            embeds=[NS(title="Forex Alert", description=description, fields=[])],
            reference=NS(message_id=ref) if ref else None,
            created_at=datetime.now(timezone.utc),
        )

    assert (
        ingest(w.store, message(100, "BUY XAUUSD CMP 3900.7\nSL: 3894.7"))[0]["queued"]
        == 1
    )
    assert (
        ingest(w.store, message(101, "SELL XAUUSD CMP 3940.4\nSL: 3951"))[0]["queued"]
        == 1
    )
    assert (
        ingest(w.store, message(102, "XAUUSD Set TP:\nTP1: 3910\nTP2: 3920", 100))[0][
            "queued"
        ]
        == 1
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        ingest(w.store, message(103, "XAUUSD Move SL to Breakeven (3900.7)"))
    assert any(
        "Discord source 103 ignored" in e["detail"]
        for e in w.store.dashboard(w.owner)["events"]
    )


def test_closing_status_releases_single_thread_context(workspace):
    w = workspace
    a = add_account(w)
    worker_ready(w, a)

    def ingest(id, text):
        return w.store.ingest(w.analyst, id, text, time.time())

    ingest("100", "BUY XAUUSD CMP 3900.7\nSL: 3894.7")
    assert ingest("101", "XAUUSD Stop Loss Hit -60 pips")["queued"] == 0
    ingest("102", "SELL XAUUSD CMP 3940.4\nSL: 3951")
    assert ingest("103", "XAUUSD Set TP:\nTP1: 3929.5\nTP2: 3924")["queued"] == 1


def test_explicitly_authorized_webhook_uses_exact_identity(workspace):
    from datetime import datetime, timezone
    from goldcopier.discord_platform import ingest

    w = workspace
    a = add_account(w)
    worker_ready(w, a)
    msg = NS(
        id=700,
        guild=NS(id=1),
        channel=NS(id=2),
        author=NS(id=3, bot=True),
        webhook_id=3,
        content="BUY XAUUSD CMP 2700\nSL: 2695",
        created_at=datetime.now(timezone.utc),
    )
    assert ingest(w.store, msg)[0]["queued"] == 1
    msg.id = 701
    msg.webhook_id = 99
    assert ingest(w.store, msg) == []


def test_no_stop_never_enters_and_foreign_references_are_rejected(workspace):
    with pytest.raises(ValueError, match="required"):
        parse_signal("BUY XAUUSD CMP 3900")
    w = workspace
    a = add_account(w)
    worker_ready(w, a)
    other = w.store.add_analyst(
        {
            "name": "Other",
            "description": "",
            "guild_id": "9",
            "channel_id": "8",
            "trader_id": "7",
            "active": True,
        }
    )
    w.store.ingest(other, "100", "BUY XAUUSD CMP 3900\nSL: 3890", time.time())
    with pytest.raises(ValueError, match="Management reference"):
        w.store.ingest(
            w.analyst, "101", "XAUUSD Set TP:\nTP: 3910\nSIGNAL: 100", time.time()
        )


def test_direct_exness_cmp_omits_missing_targets(tmp_path):
    from test_exness_broker import setup

    broker, client, ledger = setup(tmp_path)
    s = parse_signal("BUY EURUSD CMP 1.0801\nSL: 1.0781")
    s.update(signal_id="ab" * 16, broker_symbol="EURUSD")
    execute(
        {
            "id": "api-cmp",
            "signal": s,
            "risk": Risk().model_dump(),
            "expires": time.time() + 60,
        },
        broker,
        ledger,
    )
    assert len(client.calls) == 1
    assert "take_profit_price" not in client.calls[0][2]


def test_one_closed_leg_does_not_close_the_other_routing_context(workspace):
    w = workspace
    a = add_account(w)
    worker_ready(w, a)

    def ingest(id, text):
        return w.store.ingest(w.analyst, id, text, time.time())

    ingest("100", "PLACE XAUUSD BUY LIMIT\nENTRY: 3947 | 3943.5\nSL: 3934.5")
    ingest("101", "XAUUSD Trade Closed (3947.9)\nSIGNAL: 100-1")
    ingest("102", "BUY XAUUSD CMP 3900.7\nSL: 3894.7")
    with pytest.raises(ValueError, match="Ambiguous"):
        ingest("103", "XAUUSD Set TP:\nTP: 3960")
