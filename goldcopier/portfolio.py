"""Authenticated browser portfolio stream; workers remain the source of broker data."""

import asyncio
import json
import time


async def portfolio_events(store, request, session_token, user_id):
    yield "retry: 2000\n\n"
    while not await request.is_disconnected():
        session = await asyncio.to_thread(store.session, session_token)
        if not session or session["id"] != user_id:
            yield "event: expired\ndata: {}\n\n"
            return
        data = await asyncio.to_thread(store.dashboard, user_id, False)
        data.pop("equity", None)
        # Instrument catalogues are static configuration, already loaded by /dashboard.
        for account in data["accounts"]:
            account["snapshot"].pop("instruments", None)
        yield (
            "event: portfolio\ndata: "
            + json.dumps(
                {"dashboard": data, "server_time": time.time()},
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n\n"
        )
        await asyncio.sleep(1)
