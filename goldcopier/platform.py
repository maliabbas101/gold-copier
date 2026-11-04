"""Community web app. Run uvicorn goldcopier.platform:create_app --factory."""

import csv
import io
import json
import logging
import os
from pathlib import Path
import secrets
import smtplib
import sqlite3
import time
from email.message import EmailMessage

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import Vault, digest, password_hash
from .billing import Billing
from . import platform_models as M
from .platform_store import PlatformStore, dump
from .signals_v2 import parse_signal, parse_signals
from .instruments import (
    PairInput,
    MappingInput,
    save_pair,
    delete_pair,
    save_mapping,
    delete_mapping,
)

WEB = Path(__file__).parent / "web"


def create_app():
    base = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
    demo = os.environ.get("DEMO_MODE", "false").lower() == "true"
    live = os.environ.get("ALLOW_LIVE_TRADING", "false").lower() == "true"
    paid = os.environ.get("BILLING_ENABLED", "false").lower() == "true"
    if not base.startswith("https://") and not base.startswith(
        ("http://127.0.0.1:", "http://localhost:")
    ):
        raise ValueError("PUBLIC_URL must use HTTPS outside localhost")
    if demo and live:
        raise ValueError("Demo workspace cannot enable live execution")
    key = os.environ.get("VAULT_KEY", "")
    if not key:
        raise ValueError(
            "Generate and configure VAULT_KEY before starting the platform"
        )
    vault = Vault(key)
    store = PlatformStore(os.environ.get("PLATFORM_DB", "platform.db"))
    billing = Billing(store, base, paid)
    app = FastAPI(title="Gold Copier Platform", docs_url=None, redoc_url=None)
    app.state.store, app.state.billing = store, billing

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": "Record already exists or conflicts with existing data"},
            status_code=409,
        )

    @app.middleware("http")
    async def security(request, call_next):
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0")) > 65536
        ):
            return Response("Request too large", 413)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != base:
                return Response("Untrusted origin", 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api") else "no-cache"
        )
        if base.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def user(request: Request, gold_session: str | None = Cookie(default=None)):
        current = store.session(gold_session or "")
        if not current:
            raise HTTPException(401, "Sign in to continue")
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), current["csrf"]
        ):
            raise HTTPException(403, "Invalid CSRF token")
        return current

    def admin(current=Depends(user)):
        if current["role"] != "admin":
            raise HTTPException(403, "Operator access required")
        return current

    def agent(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Worker token required")
        current = store.agent(authorization[7:])
        if not current:
            raise HTTPException(403, "Invalid worker token")
        return current

    def supervisor(authorization: str = Header(default="")):
        expected = os.environ.get("SUPERVISOR_TOKEN", "")
        if len(expected) < 32 or not secrets.compare_digest(
            authorization, "Bearer " + expected
        ):
            raise HTTPException(403, "Supervisor token required")

    @app.get("/api/health")
    def health():
        result = {"ok": True, "version": "0.2.0"}
        if os.environ.get("LOCAL_RUNTIME_ID"):
            result["runtime_id"] = os.environ["LOCAL_RUNTIME_ID"]
        return result

    @app.get("/api/config")
    def config():
        return {
            "demo": demo,
            "live_enabled": live,
            "billing_enabled": paid,
            "billing_ready": bool(
                paid and billing.key and billing.secret and all(billing.prices.values())
            ),
            "master_worker_available": bool(os.environ.get("MASTER_TERMINAL_PATH")),
            "managed_available": bool(os.environ.get("SUPERVISOR_TOKEN")),
            "reset_available": bool(os.environ.get("SMTP_HOST")),
            "local_worker_available": bool(
                os.environ.get("LOCAL_RUNTIME_ID")
                and os.environ.get("MT5_TERMINAL_PATH")
            ),
        }

    @app.get("/api/demo")
    def public_demo():
        if not demo:
            raise HTTPException(404, "Demo is disabled")
        with store.db() as db:
            row = db.execute(
                "SELECT id,name FROM users WHERE email='demo@goldcopier.local'"
            ).fetchone()
        if not row:
            raise HTTPException(404, "Seed the demo workspace first")
        return {
            "user": {
                "name": row["name"],
                "email": "demo@goldcopier.local",
                "role": "member",
            },
            "dashboard": store.dashboard(row["id"]),
        }

    @app.post("/api/auth/signup")
    def signup(body: M.Signup, request: Request):
        store.limit("signup:" + request.client.host, 5, 3600)
        store.signup(body.name, body.email, body.password)
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(body: M.Credentials, request: Request, response: Response):
        store.limit("login:" + request.client.host, 20, 300)
        store.limit("email:" + body.email.lower(), 10, 300)
        token, csrf, current = store.login(body.email, body.password)
        response.set_cookie(
            "gold_session",
            token,
            httponly=True,
            secure=base.startswith("https://"),
            samesite="strict",
            max_age=86400,
            path="/",
        )
        return {"user": current, "csrf": csrf}

    @app.get("/api/auth/me")
    def me(current=Depends(user)):
        return {
            "user": {k: v for k, v in current.items() if k != "csrf"},
            "csrf": current["csrf"],
        }

    @app.post("/api/auth/logout")
    def logout(
        response: Response,
        gold_session: str | None = Cookie(default=None),
        current=Depends(user),
    ):
        with store.db() as db:
            db.execute(
                "DELETE FROM sessions WHERE token=?", (digest(gold_session or ""),)
            )
        response.delete_cookie("gold_session", path="/")
        return {"ok": True}

    @app.post("/api/auth/reset-request")
    def request_reset(body: M.ResetRequest, request: Request):
        store.limit("reset:" + request.client.host, 3, 3600)
        if not os.environ.get("SMTP_HOST"):
            raise HTTPException(503, "Password reset email is not configured")
        with store.db() as db:
            row = db.execute(
                "SELECT id FROM users WHERE email=?", (body.email.lower().strip(),)
            ).fetchone()
            if row:
                token = secrets.token_urlsafe(48)
                db.execute("DELETE FROM resets WHERE user_id=?", (row["id"],))
                db.execute(
                    "INSERT INTO resets VALUES(?,?,?)",
                    (digest(token), row["id"], time.time() + 1800),
                )
        if row:
            message = EmailMessage()
            message["Subject"] = "Reset your Gold Copier password"
            message["From"] = os.environ["SMTP_FROM"]
            message["To"] = body.email
            message.set_content(
                "Use this link within 30 minutes: " + base + "/#reset/" + token
            )
            try:
                with smtplib.SMTP(
                    os.environ["SMTP_HOST"],
                    int(os.environ.get("SMTP_PORT", "587")),
                    timeout=10,
                ) as smtp:
                    smtp.starttls()
                    if os.environ.get("SMTP_USER"):
                        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                    smtp.send_message(message)
            except Exception:
                logging.exception("Password reset delivery failed")
        return {
            "ok": True,
            "message": "If the account exists, a reset email will be sent.",
        }

    @app.post("/api/auth/reset")
    def reset(body: M.ResetPassword):
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM resets WHERE token=? AND expires>?",
                (digest(body.token), time.time()),
            ).fetchone()
            if not row:
                raise ValueError("Reset link is invalid or expired")
            db.execute(
                "UPDATE users SET password=? WHERE id=?",
                (password_hash(body.password), row["user_id"]),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
            db.execute("DELETE FROM resets WHERE user_id=?", (row["user_id"],))
            store.event(
                db, row["user_id"], "security", "Password reset; all sessions revoked"
            )
        return {"ok": True}

    @app.get("/api/dashboard")
    def dashboard(current=Depends(user)):
        return {**store.dashboard(current["id"]), "server_time": time.time()}

    @app.get("/api/portfolio/stream")
    async def portfolio_stream(request: Request, current=Depends(user)):
        from .portfolio import portfolio_events

        return StreamingResponse(
            portfolio_events(
                store, request, request.cookies.get("gold_session", ""), current["id"]
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/portfolio/equity")
    def portfolio_equity(account_id: str, period: str = "LIVE", current=Depends(user)):
        return {
            "points": store.equity_history(current["id"], account_id, period),
            "server_time": time.time(),
        }

    @app.get("/api/pairs")
    def pairs(current=Depends(user)):
        from .instruments import registry

        with store.db() as db:
            return registry(db)

    @app.post("/api/operator/pairs")
    def pair_create(body: PairInput, current=Depends(admin)):
        save_pair(store, body.model_dump())
        return {"ok": True}

    @app.put("/api/operator/pairs/{symbol}")
    def pair_update(symbol: str, body: PairInput, current=Depends(admin)):
        save_pair(store, body.model_dump(), symbol)
        return {"ok": True}

    @app.delete("/api/operator/pairs/{symbol}")
    def pair_delete(symbol: str, current=Depends(admin)):
        delete_pair(store, symbol)
        return {"ok": True}

    @app.get("/api/accounts/{account_id}/pairs")
    def pair_mappings(account_id: str, current=Depends(user)):
        with store.db() as db:
            a = store.account(current["id"], account_id, db)
            return {
                "mappings": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM account_pairs WHERE account_id=?", (account_id,)
                    )
                ],
                "instruments": json.loads(a["snapshot"] or "{}").get("instruments", []),
            }

    @app.post("/api/accounts/{account_id}/pairs")
    def mapping_create(account_id: str, body: MappingInput, current=Depends(user)):
        save_mapping(store, current["id"], account_id, body.model_dump())
        return {"ok": True}

    @app.put("/api/accounts/{account_id}/pairs/{pair}")
    def mapping_update(
        account_id: str, pair: str, body: MappingInput, current=Depends(user)
    ):
        save_mapping(store, current["id"], account_id, body.model_dump(), pair)
        return {"ok": True}

    @app.delete("/api/accounts/{account_id}/pairs/{pair}")
    def mapping_delete(account_id: str, pair: str, current=Depends(user)):
        delete_mapping(store, current["id"], account_id, pair)
        return {"ok": True}

    @app.post("/api/accounts/exness")
    def add_exness(body: M.ExnessAccountInput, current=Depends(user)):
        if body.purpose == "master" and current["role"] != "admin":
            raise HTTPException(403, "Only the operator can connect a trader source")
        if demo:
            raise ValueError("Demo workspaces cannot collect Exness credentials")
        from .exness_api import host, signing_key

        signing_key(body.private_key)
        gateway = host(body.gateway)
        account_id = store.add_account(
            current["id"],
            {
                "name": body.name,
                "broker": "Exness",
                "login": body.account_id,
                "server": "Exness API",
                "symbol": "XAUUSD",
                "mode": "live",
                "purpose": body.purpose,
                "hosting": "api",
                "password": json.dumps(
                    {
                        "api_key": body.api_key,
                        "private_key": body.private_key,
                        "gateway": gateway,
                    }
                ),
            },
            vault,
        )
        with store.db() as db:
            db.execute(
                "UPDATE accounts SET connector='exness' WHERE id=?", (account_id,)
            )
        return {"id": account_id, "read_only": body.purpose == "master" or not live}

    @app.post("/api/accounts")
    def add_account(body: M.AccountInput, current=Depends(user)):
        if body.purpose == "master" and current["role"] != "admin":
            raise HTTPException(403, "Only the operator can connect a trader source")
        if body.mode == "live" and not live and body.purpose != "master":
            raise ValueError("Live accounts are disabled for this installation")
        if body.hosting == "managed" and not os.environ.get("SUPERVISOR_TOKEN"):
            raise ValueError("Managed hosting is not configured")
        if demo and body.hosting == "managed":
            raise ValueError("Demo workspace cannot collect broker passwords")
        return {"id": store.add_account(current["id"], body.model_dump(), vault)}

    @app.post("/api/accounts/{account_id}/token")
    def rotate(account_id: str, current=Depends(user)):
        return {"token": store.rotate_agent(current["id"], account_id)}

    @app.post("/api/accounts/{account_id}/enabled")
    def enable(account_id: str, body: M.Toggle, current=Depends(user)):
        store.set_enabled(current["id"], account_id, body.enabled, live, paid)
        return {"ok": True}

    @app.put("/api/accounts/{account_id}/risk")
    def risk(account_id: str, body: M.Risk, current=Depends(user)):
        store.set_risk(current["id"], account_id, body.model_dump())
        return {"ok": True}

    @app.put("/api/accounts/{account_id}/analysts/{analyst_id}/risk")
    def analyst_risk(
        account_id: str, analyst_id: str, body: M.Risk, current=Depends(user)
    ):
        store.set_risk(current["id"], account_id, body.model_dump(), analyst_id)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/analysts/{analyst_id}")
    def follow(account_id: str, analyst_id: str, body: M.Toggle, current=Depends(user)):
        store.follow(current["id"], account_id, analyst_id, body.enabled)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/commands")
    def control(account_id: str, body: M.Control, current=Depends(user)):
        return {
            "id": store.control(
                current["id"], account_id, body.model_dump(exclude_none=True)
            )
        }

    @app.post("/api/accounts/{account_id}/disconnect")
    def disconnect_account(
        account_id: str, body: M.AccountRemoval, current=Depends(user)
    ):
        store.disconnect_account(current["id"], account_id)
        return {"ok": True}

    @app.delete("/api/accounts/{account_id}")
    def remove_account(account_id: str, body: M.AccountRemoval, current=Depends(user)):
        store.disconnect_account(current["id"], account_id, delete=True)
        return {"ok": True}

    @app.get("/api/trades/export")
    def export(current=Depends(user)):
        rows = store.dashboard(current["id"])["jobs"]
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["job", "account", "action", "state", "created", "detail"])

        def safe(v):
            s = str(v)
            return "'" + s if s.startswith(("=", "+", "-", "@", "\t", "\r")) else s

        for r in rows:
            writer.writerow(
                [
                    safe(r["id"]),
                    safe(r["account_id"]),
                    safe(r["payload"]["action"]),
                    safe(r["state"]),
                    r["created"],
                    safe((r["result"] or {}).get("detail", "")),
                ]
            )
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=gold-copier-trades.csv"
            },
        )

    @app.post("/api/billing/checkout")
    def checkout(body: M.Checkout, current=Depends(user)):
        return {"url": billing.checkout(current, body.plan, body.return_to)}

    @app.post("/api/billing/portal")
    def portal(current=Depends(user)):
        return {"url": billing.portal(current)}

    @app.post("/api/billing/webhook")
    async def webhook(request: Request):
        try:
            billing.webhook(
                await request.body(), request.headers.get("stripe-signature", "")
            )
        except ValueError:
            raise
        except Exception as exc:
            logging.exception("Stripe webhook rejected")
            raise HTTPException(
                400, "Webhook verification or synchronization failed"
            ) from exc
        return {"received": True}

    def worker_pairs(account_id):
        with store.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT m.* FROM account_pairs m JOIN pairs p ON p.symbol=m.pair WHERE m.account_id=? AND m.enabled=1 AND p.enabled=1",
                    (account_id,),
                )
            ]

    @app.get("/api/worker/config")
    def worker_config(a=Depends(agent)):
        if (
            a["mode"] == "live"
            and not live
            and a.get("connector") != "exness"
            and a.get("purpose") != "master"
        ):
            raise ValueError("Live execution disabled")
        from .source_platform import source_config

        with store.db() as db:
            source = source_config(db, a["id"])
            child = db.execute(
                "SELECT b.id child_id,c.id parent_id,c.name master_name FROM community_bots b JOIN communities c ON c.id=b.community_id WHERE b.account_id=?",
                (a["id"],),
            ).fetchone()
            parent = db.execute(
                "SELECT c.id parent_id,c.name master_name FROM communities c JOIN master_sources s ON s.analyst_id=c.analyst_id WHERE s.account_id=?",
                (a["id"],),
            ).fetchone()
        return {
            "purpose": a.get("purpose", "follower"),
            "master_source": source,
            "bot_context": dict(child) if child else dict(parent) if parent else None,
            "account_id": a["id"],
            "hosting": a["hosting"],
            "connector": a.get("connector", "mt5"),
            "api_credentials": json.loads(vault.decrypt(a["secret"], a["id"]))
            if a.get("connector") == "exness" and a["secret"]
            else None,
            "login": a["login"],
            "server": a["server"],
            "symbol": a["symbol"],
            "mode": a["mode"],
            "enabled": bool(a["enabled"]),
            "risk": json.loads(a["risk"]),
            "pairs": worker_pairs(a["id"]),
            "password": vault.decrypt(a["secret"], a["id"])
            if a["hosting"] == "managed" and a["secret"]
            else None,
            "allow_live": live and a.get("purpose") != "master",
            "demo_workspace": demo,
        }

    @app.post("/api/worker/master-events")
    def master_events(body: M.Ingest, a=Depends(agent)):
        from .source_platform import source_config

        with store.db() as db:
            source = source_config(db, a["id"])
        if (
            demo
            or a.get("purpose") != "master"
            or not source
            or not source["enabled"]
            or not source["active"]
            or source["analyst_id"] != body.analyst_id
        ):
            raise HTTPException(
                403, "This worker has no active trader source for that analyst"
            )
        return store.ingest(
            body.analyst_id,
            body.source_id,
            body.text,
            body.created,
            billing=paid,
            origin="broker",
            master_entry=body.master_entry,
        )

    @app.get("/api/worker/master-events/{source_id}")
    def master_receipt(source_id: str, a=Depends(agent)):
        from .source_platform import source_config

        with store.db() as db:
            source = source_config(db, a["id"])
            if a.get("purpose") != "master" or not source:
                raise HTTPException(403, "Trader source worker required")
            processed = db.execute(
                "SELECT 1 FROM signal_messages WHERE analyst_id=? AND source_id=?",
                (source["analyst_id"], source_id),
            ).fetchone()
        return {"processed": bool(processed)}

    @app.post("/api/operator/master-sources")
    def set_master_source(body: M.SourceBinding, current=Depends(admin)):
        from .source_platform import configure_source

        return configure_source(store, current["id"], body.model_dump())

    @app.post("/api/worker/heartbeat")
    def heartbeat(body: M.Snapshot, a=Depends(agent)):
        store.heartbeat(a, body.model_dump())
        from .community import auto_start

        auto_start(store, a["id"], paid, live)
        return {"ok": True}

    @app.post("/api/worker/claim")
    def claim(a=Depends(agent)):
        if demo or a.get("purpose") == "master":
            return None
        if a["mode"] == "live" and not live:
            return None
        return store.claim(a, paid)

    @app.post("/api/worker/jobs/{job_id}/result")
    def result(job_id: str, body: M.JobResult, a=Depends(agent)):
        store.result(a, job_id, body.model_dump())
        return {"ok": True}

    @app.post("/api/worker/auto-result")
    def auto_result(body: M.AutoResult, a=Depends(agent)):
        with store.db() as db:
            original = db.execute(
                "SELECT 1 FROM pjobs WHERE account_id=? AND feed_id=? AND json_extract(payload,'$.action')='entry'",
                (a["id"], body.signal_id),
            ).fetchone()
            if not original:
                raise ValueError("Automatic exit has no owned entry reference")
            db.execute(
                "INSERT OR IGNORE INTO pjobs(id,account_id,feed_id,payload,risk,state,created,expires,claimed) VALUES(?,?,?,?,?,'claimed',?,?,?)",
                (
                    body.id,
                    a["id"],
                    body.id,
                    dump(
                        {
                            "action": "partial",
                            "signal_id": body.signal_id,
                            "automatic": True,
                        }
                    ),
                    a["risk"],
                    time.time(),
                    time.time() + 60,
                    time.time(),
                ),
            )
        store.result(a, body.id, body.result.model_dump())
        return {"ok": True}

    @app.get("/api/worker/reconciled")
    def reconciled(ids: str = "", a=Depends(agent)):
        wanted = ids.split(",")[:100]
        with store.db() as db:
            rows = db.execute(
                "SELECT id,state,result FROM pjobs WHERE account_id=? AND state IN ('executed','rejected') AND id IN ("
                + ",".join("?" for _ in wanted)
                + ")",
                [a["id"], *wanted],
            ).fetchall()
            return [dict(r) for r in rows]

    @app.post("/api/ingest")
    def service_ingest(body: M.Ingest, authorization: str = Header(default="")):
        expected = os.environ.get("SIGNAL_INGEST_TOKEN", "")
        if len(expected) < 32 or not secrets.compare_digest(
            authorization, "Bearer " + expected
        ):
            raise HTTPException(403, "Signal service token required")
        return store.ingest(
            body.analyst_id, body.source_id, body.text, body.created, billing=paid
        )

    @app.get("/api/operator/status")
    def operator(current=Depends(admin)):
        with store.db() as db:
            accounts = [
                {k: v for k, v in dict(r).items() if k not in ("secret", "agent_hash")}
                for r in db.execute(
                    "SELECT a.*,u.email FROM accounts a JOIN users u ON u.id=a.user_id"
                )
            ]
            unresolved = [
                dict(r)
                for r in db.execute(
                    "SELECT id,account_id,state,created,result FROM pjobs WHERE state IN ('claimed','uncertain')"
                )
            ]
            events = [
                dict(r)
                for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")
            ]
            analysts = [dict(r) for r in db.execute("SELECT * FROM analysts")]
            sources = [
                dict(r)
                for r in db.execute(
                    "SELECT s.*,a.name account_name,a.heartbeat,a.disconnected,json_extract(a.snapshot,'$.source_status') source_status FROM master_sources s JOIN accounts a ON a.id=s.account_id"
                )
            ]
            logs = [
                dict(r)
                for r in db.execute(
                    "SELECT id,analyst_id,state,attempts,last_error,created,sent FROM discord_outbox ORDER BY created DESC LIMIT 30"
                )
            ]
        for account in accounts:
            account["owned"] = account["user_id"] == current["id"]
        return {
            "sources": sources,
            "discord_logs": logs,
            "discord_logging_configured": bool(os.environ.get("DISCORD_BOT_TOKEN")),
            "accounts": accounts,
            "unresolved": unresolved,
            "events": events,
            "analysts": analysts,
        }

    @app.get("/api/operator/communities")
    def operator_fleet(current=Depends(admin)):
        from .community import fleet

        return fleet(store)

    @app.post("/api/operator/analysts")
    def analyst(body: M.Analyst, current=Depends(admin)):
        return {"id": store.add_analyst(body.model_dump())}

    @app.post("/api/operator/analysts/{analyst_id}/enabled")
    def analyst_toggle(analyst_id: str, body: M.Toggle, current=Depends(admin)):
        with store.db() as db:
            if not db.execute(
                "SELECT 1 FROM analysts WHERE id=?", (analyst_id,)
            ).fetchone():
                raise ValueError("Analyst not found")
            db.execute(
                "UPDATE analysts SET active=? WHERE id=?",
                (int(body.enabled), analyst_id),
            )
            store.event(
                db,
                None,
                "admin",
                "Analyst feed " + ("enabled" if body.enabled else "disabled"),
            )
        return {"ok": True}

    @app.post("/api/operator/pause")
    def pause(body: M.Toggle, current=Depends(admin)):
        with store.db() as db:
            db.execute(
                "UPDATE settings SET value=? WHERE key='paused'",
                ("true" if body.enabled else "false",),
            )
            if body.enabled:
                db.execute(
                    "UPDATE pjobs SET state='cancelled' WHERE state='queued' AND json_extract(payload,'$.action')='entry'"
                )
            store.event(db, None, "admin", "Global pause " + str(body.enabled))
        return {"ok": True}

    @app.post("/api/operator/preview")
    def preview(body: M.Ingest, current=Depends(admin)):
        signals = parse_signals(body.text)
        return signals[0] if len(signals) == 1 else {"signals": signals}

    @app.post("/api/operator/signals")
    def ingest(body: M.Ingest, current=Depends(admin)):
        return store.ingest(
            body.analyst_id, body.source_id, body.text, body.created, billing=paid
        )

    @app.post("/api/operator/jobs/{job_id}/resolve")
    def resolve(job_id: str, body: M.Resolve, current=Depends(admin)):
        store.resolve(job_id, body.state, body.note, current["email"])
        return {"ok": True}

    @app.get("/api/supervisor/assignments", dependencies=[Depends(supervisor)])
    def assignments():
        with store.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,user_id,name,mode,heartbeat,connector,hosting FROM accounts WHERE hosting IN ('managed','managed_api') AND secret IS NOT NULL AND disconnected=0"
                )
            ]

    @app.post(
        "/api/supervisor/accounts/{account_id}/status",
        dependencies=[Depends(supervisor)],
    )
    def hosting_status(account_id: str, body: M.HostingStatus):
        with store.db() as db:
            a = db.execute(
                "SELECT * FROM accounts WHERE id=? AND hosting IN ('managed','managed_api')",
                (account_id,),
            ).fetchone()
            if not a:
                raise ValueError("Hosted account not found")
            snap = json.loads(a["snapshot"] or "{}")
            snap["error"] = body.error
            if a["purpose"] == "master":
                snap["source_status"] = body.error
            db.execute(
                "UPDATE accounts SET enabled=0,heartbeat=0,snapshot=? WHERE id=?",
                (json.dumps(snap), account_id),
            )
            store.event(db, a["user_id"], "alert", body.error, account_id)
        return {"ok": True}

    @app.post(
        "/api/supervisor/accounts/{account_id}/token",
        dependencies=[Depends(supervisor)],
    )
    def assignment_token(account_id: str):
        with store.db() as db:
            a = db.execute(
                "SELECT user_id FROM accounts WHERE id=? AND hosting IN ('managed','managed_api') AND secret IS NOT NULL AND disconnected=0",
                (account_id,),
            ).fetchone()
        if not a:
            raise ValueError("Managed assignment not found")
        return {"token": store.rotate_agent(a["user_id"], account_id)}

    from .community_routes import install as install_community_routes

    install_community_routes(app, store, vault, billing, base, user, paid, live, demo)

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    return app
