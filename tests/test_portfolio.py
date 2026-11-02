import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest

from test_platform import workspace, add_account, worker_ready
from test_worker_v2 import connected_v2
from goldcopier.portfolio import portfolio_events


def snapshot(agent, equity=10000):
    return {
        "login": agent["login"],
        "server": agent["server"],
        "mode": agent["mode"],
        "balance": 10000,
        "equity": equity,
        "currency": "USD",
        "positions": [],
        "orders": [],
        "daily_pnl": 0,
    }


def test_stream_pushes_updates_and_stops_after_session_revocation(
    workspace, monkeypatch
):
    w = workspace
    a = add_account(w)
    token, agent = worker_ready(w, a)
    other = add_account(w, login="87654321")
    with w.store.db() as db:
        db.execute("UPDATE accounts SET user_id=? WHERE id=?", (w.other, other))

    async def disconnected():
        return False

    async def no_sleep(_):
        pass

    monkeypatch.setattr("goldcopier.portfolio.asyncio.sleep", no_sleep)

    async def run():
        events = portfolio_events(
            w.store,
            NS(is_disconnected=disconnected),
            w.client.cookies.get("gold_session"),
            w.owner,
        )
        assert "retry:" in await anext(events)
        first = await anext(events)
        payload = json.loads(first.split("data: ", 1)[1])
        assert [r["id"] for r in payload["dashboard"]["accounts"]] == [a]
        assert "equity" not in payload["dashboard"]
        assert "agent_hash" not in first and "secret" not in first
        w.store.heartbeat(agent, snapshot(agent, 10123.45))
        second = json.loads((await anext(events)).split("data: ", 1)[1])
        assert second["dashboard"]["accounts"][0]["snapshot"]["equity"] == 10123.45
        with w.store.db() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (w.owner,))
        assert "event: expired" in await anext(events)
        with pytest.raises(StopAsyncIteration):
            await anext(events)

    asyncio.run(run())


def test_stream_and_history_require_authentication_and_account_ownership(workspace):
    w = workspace
    a = add_account(w)
    assert (
        w.client.get(
            "/api/portfolio/equity", params={"account_id": a, "period": "LIVE"}
        ).status_code
        == 200
    )
    with w.store.db() as db:
        db.execute("UPDATE accounts SET user_id=? WHERE id=?", (w.other, a))
    assert (
        w.client.get("/api/portfolio/equity", params={"account_id": a}).status_code
        == 400
    )
    w.client.cookies.clear()
    assert w.client.get("/api/portfolio/stream").status_code == 401
    assert (
        w.client.get("/api/portfolio/equity", params={"account_id": a}).status_code
        == 401
    )


def test_history_ranges_are_per_account_and_preserve_the_last_bucket_value(workspace):
    w = workspace
    a = add_account(w)
    other = add_account(w, login="87654321")
    now = time.time()
    with w.store.db() as db:
        for i in range(200):
            db.execute(
                "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
                (a, 10000, 10000 + i, now - 1000 + i * 5),
            )
            db.execute(
                "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
                (other, 20000, 20000 + i, now - 1000 + i * 5),
            )
        db.execute(
            "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
            (a, 10000, 9990, now - 10 * 86400),
        )
    live = w.store.equity_history(w.owner, a, "LIVE")
    month = w.store.equity_history(w.owner, a, "30D")
    assert len(live) == 200
    assert all(p["account_id"] == a for p in live + month)
    assert live[-1]["equity"] == 10199
    assert any(p["equity"] == 9990 for p in month)
    assert not any(p["equity"] == 9990 for p in live)
    with pytest.raises(ValueError, match="Unknown equity period"):
        w.store.equity_history(w.owner, a, "1Y")


def test_equity_is_recorded_every_five_seconds_even_when_copying_is_paused(
    workspace, monkeypatch
):
    w = workspace
    a = add_account(w)
    token, agent = worker_ready(w, a)
    w.store.set_enabled(w.owner, a, False)
    clock = [time.time()]
    monkeypatch.setattr("goldcopier.platform_store.time.time", lambda: clock[0])
    clock[0] += 1
    w.store.heartbeat(agent, snapshot(agent, 10001))
    clock[0] += 5
    w.store.heartbeat(
        agent, {**snapshot(agent, 10002), "error": "Daily equity loss stop reached"}
    )
    rows = w.store.equity_history(w.owner, a, "LIVE")
    assert len(rows) == 2
    assert rows[-1]["equity"] == 10002
    assert w.store.dashboard(w.owner)["accounts"][0]["snapshot"]["equity"] == 10002


def test_compaction_retains_latest_minute_values_and_is_account_scoped(workspace):
    w = workspace
    a = add_account(w)
    token, agent = worker_ready(w, a)
    other = add_account(w, login="87654321")
    old = int((time.time() - 2 * 86400) / 60) * 60
    with w.store.db() as db:
        db.execute("DELETE FROM settings WHERE key=?", ("equity_compact:" + a,))
        for index in range(3):
            db.execute(
                "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
                (a, 10000, 10000 + index, old + index),
            )
        db.execute(
            "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
            (a, 10000, 9000, time.time() - 31 * 86400),
        )
        db.execute(
            "INSERT INTO equity(account_id,balance,equity,created) VALUES(?,?,?,?)",
            (other, 10000, 8000, time.time() - 31 * 86400),
        )
    w.store.heartbeat(agent, snapshot(agent))
    with w.store.db() as db:
        oldrows = db.execute(
            "SELECT equity FROM equity WHERE account_id=? AND created<?",
            (a, time.time() - 86400),
        ).fetchall()
        assert [r[0] for r in oldrows] == [10002]
        assert db.execute(
            "SELECT 1 FROM equity WHERE account_id=?", (other,)
        ).fetchone()


def test_mt5_snapshots_include_current_price_and_profit(connected_v2):
    broker, mt = connected_v2
    mt.positions = (
        NS(
            ticket=1,
            symbol="XAUUSDm",
            type=0,
            volume=0.01,
            price_open=2700,
            price_current=2702.5,
            sl=2690,
            tp=2720,
            profit=2.5,
            magic=740127,
        ),
    )
    data = broker.snapshot()
    assert data["positions"][0]["price_current"] == 2702.5
    assert data["positions"][0]["profit"] == 2.5


def test_authenticated_sse_reaches_browser_over_http_without_response_buffering(
    workspace,
):
    import socket
    import threading
    import httpx
    import uvicorn

    w = workspace
    a = add_account(w)
    worker_ready(w, a)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(w.app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=5,
            trust_env=False,
            cookies={"gold_session": w.client.cookies.get("gold_session")},
        ) as client:
            with client.stream("GET", "/api/portfolio/stream") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["x-accel-buffering"] == "no"
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        assert data["dashboard"]["accounts"][0]["id"] == a
                        break
                else:
                    pytest.fail("SSE ended without a portfolio frame")
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
    assert not thread.is_alive()
