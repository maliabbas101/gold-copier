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
from .signals_v2 import parse_signal

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
            "managed_available": bool(os.environ.get("SUPERVISOR_TOKEN")),
            "reset_available": bool(os.environ.get("SMTP_HOST")),
            "local_worker_available": bool(
                os.environ.get("LOCAL_RUNTIME_ID") and os.environ.get("MT5_TERMINAL_PATH")
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
        return store.dashboard(current["id"])

    @app.post("/api/accounts")
    def add_account(body: M.AccountInput, current=Depends(user)):
        if body.mode == "live" and not live:
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

    @app.delete("/api/accounts/{account_id}")
    def remove_account(account_id: str, current=Depends(user)):
        with store.db() as db:
            a = store.account(current["id"], account_id, db)
            snap = json.loads(a["snapshot"] or "{}")
            if db.execute(
                "SELECT 1 FROM pjobs WHERE account_id=? AND state IN ('claimed','uncertain')",
                (account_id,),
            ).fetchone():
                raise ValueError("Reconcile in-flight commands before disconnecting")
            if (
                a["enabled"]
                or not a["heartbeat"]
                or time.time() - a["heartbeat"] > 30
                or snap.get("positions")
                or snap.get("orders")
            ):
                raise ValueError(
                    "Pause copying and verify a fresh empty broker account before disconnecting"
                )
            db.execute(
                "UPDATE accounts SET agent_hash=NULL,secret=NULL,enabled=0 WHERE id=?",
                (account_id,),
            )
            store.event(
                db,
                current["id"],
                "security",
                "Account disconnected; credentials and worker token revoked",
                account_id,
            )
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
        return {"url": billing.checkout(current, body.plan)}

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

    @app.get("/api/worker/config")
    def worker_config(a=Depends(agent)):
        if a["mode"] == "live" and not live:
            raise ValueError("Live execution disabled")
        return {
            "account_id": a["id"],
            "hosting": a["hosting"],
            "login": a["login"],
            "server": a["server"],
            "symbol": a["symbol"],
            "mode": a["mode"],
            "enabled": bool(a["enabled"]),
            "risk": json.loads(a["risk"]),
            "password": vault.decrypt(a["secret"], a["id"])
            if a["hosting"] == "managed" and a["secret"] else None,
            "allow_live": live,
            "demo_workspace": demo,
        }

    @app.post("/api/worker/heartbeat")
    def heartbeat(body: M.Snapshot, a=Depends(agent)):
        store.heartbeat(a, body.model_dump())
        return {"ok": True}

    @app.post("/api/worker/claim")
    def claim(a=Depends(agent)):
        if demo:
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
        return {
            "accounts": accounts,
            "unresolved": unresolved,
            "events": events,
            "analysts": analysts,
        }

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
        return parse_signal(body.text)

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
                    "SELECT id,user_id,name,mode,heartbeat FROM accounts WHERE hosting='managed' AND secret IS NOT NULL"
                )
            ]

    @app.post(
        "/api/supervisor/accounts/{account_id}/token",
        dependencies=[Depends(supervisor)],
    )
    def assignment_token(account_id: str):
        with store.db() as db:
            a = db.execute(
                "SELECT user_id FROM accounts WHERE id=? AND hosting='managed' AND secret IS NOT NULL",
                (account_id,),
            ).fetchone()
        if not a:
            raise ValueError("Managed assignment not found")
        return {"token": store.rotate_agent(a["user_id"], account_id)}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    return app
