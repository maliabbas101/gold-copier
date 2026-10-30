"""Single-machine server, Discord listener and dedicated local MT5 worker."""

import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from .auth import Vault
from .platform_models import Analyst, Signup
from .platform_store import PlatformStore

log = logging.getLogger(__name__)


def load_env(path=".env", environ=None):
    """Read literal KEY=value settings; shell variables take precedence.

    Quotes are stripped, but Windows backslashes are preserved. No shell
    expansion, interpolation or execution is performed.
    """
    environ = os.environ if environ is None else environ
    path = Path(path)
    if not path.exists():
        return
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier() or not key.isascii():
            raise ValueError(f"Invalid .env setting on line {number}")
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
                raise ValueError(f"Invalid .env quoting on line {number}")
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    for key, value in values.items():
        environ.setdefault(key, value)


def private_file(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(value)


def configure():
    if os.environ.get("DEMO_MODE", "false").lower() == "true":
        raise ValueError("Use launch --demo for the preview; manage run needs DEMO_MODE=false")
    base = (os.environ.get("PUBLIC_URL") or "http://127.0.0.1:8001").rstrip("/")
    worker_url = os.environ.get("COPIER_URL", "").strip().rstrip("/")
    if worker_url and worker_url != base:
        raise ValueError("For manage run, COPIER_URL must match PUBLIC_URL (or be blank). To connect to a separate running server, use python -m goldcopier.platform_agent.")
    parsed = urlparse(base)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.path or parsed.query or parsed.fragment or parsed.username):
        raise ValueError("manage run is local-only: use PUBLIC_URL=http://127.0.0.1:8001")
    port = parsed.port or 80
    state = Path(os.environ.get("LOCAL_STATE_DIR") or ".local-runtime").resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = Path(os.environ.get("PLATFORM_DB") or state / "platform.db").resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    # Never attach to another server that happens to be using this port.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ValueError(f"Port {port} is already in use. Stop that server or change PUBLIC_URL.") from exc
    terminal = os.environ.get("MT5_TERMINAL_PATH", "").strip()
    if terminal:
        if sys.platform != "win32":
            raise ValueError("The local MT5 worker requires Windows")
        if not Path(terminal).is_file():
            raise ValueError("MT5_TERMINAL_PATH must point to your terminal64.exe")
        if importlib.util.find_spec("MetaTrader5") is None:
            raise ValueError('Install MT5 support first: python -m pip install -e ".[mt5]"')
    store = PlatformStore(database)
    with store.db() as db:
        if db.execute("SELECT 1 FROM users WHERE email='demo@goldcopier.local'").fetchone():
            raise ValueError("This is a seeded preview database. Set PLATFORM_DB to a new database for MT5 testing.")
        encrypted = db.execute("SELECT id,secret FROM accounts WHERE secret IS NOT NULL").fetchall()
    key = os.environ.get("VAULT_KEY", "").strip()
    keyfile = state / "vault-key"
    if not key:
        if not keyfile.exists():
            if encrypted:
                raise ValueError("Existing encrypted credentials require their original VAULT_KEY; no new key was generated.")
            import base64
            private_file(keyfile, base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        key = keyfile.read_text(encoding="utf-8").strip()
    vault = Vault(key)
    for row in encrypted:
        try:
            vault.decrypt(row["secret"], row["id"])
        except Exception as exc:
            raise ValueError("VAULT_KEY does not decrypt existing credentials. Restore the original key.") from exc
    os.environ.update(
        PUBLIC_URL=base, COPIER_URL=base, PLATFORM_DB=str(database),
        VAULT_KEY=key, DEMO_MODE="false", LOCAL_RUNTIME_ID=secrets.token_hex(16),
    )
    os.environ.setdefault("ALLOW_LIVE_TRADING", "false")
    os.environ.setdefault("BILLING_ENABLED", "false")
    return store, state, base, port, terminal


def bootstrap_admin(store):
    with store.db() as db:
        if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            return
    if not os.environ.get("ADMIN_EMAIL") or not os.environ.get("ADMIN_PASSWORD"):
        raise ValueError("First start: set ADMIN_EMAIL and ADMIN_PASSWORD (at least 12 characters) in .env")
    try:
        body = Signup(
            name=os.environ.get("ADMIN_NAME") or "Local operator",
            email=os.environ["ADMIN_EMAIL"].strip(), password=os.environ["ADMIN_PASSWORD"],
        )
    except ValidationError as exc:
        # ValidationError's default string includes submitted values/passwords.
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors())
        raise ValueError(f"Invalid operator {fields} in .env. Use a valid email, a name of 2-80 characters and a password of 12-128 characters.") from None
    with store.db() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (body.email.lower().strip(),)).fetchone():
            raise ValueError("That email already exists. Create an operator using manage create-admin with a different email.")
    store.signup(body.name, body.email, body.password, "admin")
    log.info("Operator account created. Sign in using ADMIN_EMAIL and ADMIN_PASSWORD from .env.")


def bootstrap_analyst(store):
    fields = {key: os.environ.get("DISCORD_" + key.upper(), "").strip()
              for key in ("guild_id", "channel_id", "trader_id")}
    if not any(fields.values()):
        return
    try:
        analyst = Analyst(name=os.environ.get("ANALYST_NAME") or "My signal feed", **fields)
    except ValidationError:
        raise ValueError("Set all three numeric Discord IDs in .env: DISCORD_GUILD_ID, DISCORD_CHANNEL_ID and DISCORD_TRADER_ID") from None
    with store.db() as db:
        existing = db.execute(
            "SELECT id FROM analysts WHERE guild_id=? AND channel_id=? AND trader_id=?",
            (analyst.guild_id, analyst.channel_id, analyst.trader_id),
        ).fetchall()
    if len(existing) > 1:
        raise ValueError("Duplicate analyst sources exist. Keep one configured analyst for these Discord IDs.")
    if existing:
        return existing[0]["id"]
    return store.add_analyst(analyst.model_dump())


def start_child(module, *args, env=None):
    options = {"env": env or os.environ.copy()}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [sys.executable, "-m", "goldcopier.runtime_child", module, *args], **options
    )


def stop_child(child):
    if child.poll() is not None:
        return True
    try:
        if sys.platform == "win32":
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
            log.error("A service needed forced shutdown. Verify MT5 and worker locks before restarting.")
        return True
    except (OSError, subprocess.TimeoutExpired):
        log.error("Could not stop a service. Verify its process before restarting.")
        return child.poll() is not None


def wait_for_server(child, base, runtime_id, timeout=30):
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ValueError("The web server stopped during startup; inspect the error above")
            try:
                response = client.get(base + "/api/health")
                if response.status_code == 200 and response.json().get("runtime_id") == runtime_id:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.2)
    raise ValueError("Timed out waiting for this command's web server")


class LocalWorker:
    """Bind one terminal to one self-hosted account; never restart failed jobs."""

    def __init__(self, store, state, terminal):
        self.store, self.state, self.terminal = store, state, terminal
        self.child = None
        self.notice = None
        self.account_id = None
        self.failed = False

    def tick(self):
        if self.child is not None:
            if self.child.poll() is not None and not self.failed:
                self.failed = True
                detail = "MT5 connection stopped. Check the terminal login, server and trading permissions, then restart the server command."
                with self.store.db() as db:
                    row = db.execute("SELECT * FROM accounts WHERE id=?", (self.account_id,)).fetchone()
                    if row:
                        snapshot = json.loads(row["snapshot"] or "{}")
                        snapshot["error"] = detail
                        db.execute("UPDATE accounts SET enabled=0,heartbeat=0,snapshot=? WHERE id=?",
                                   (json.dumps(snapshot), self.account_id))
                        db.execute("UPDATE pjobs SET state='cancelled' WHERE account_id=? AND state='queued' AND json_extract(payload,'$.action')='entry'",
                                   (self.account_id,))
                        self.store.event(db, row["user_id"], "alert", detail, self.account_id)
                log.error("%s The app remains available; the worker was not automatically restarted.", detail)
            return
        selected = os.environ.get("LOCAL_ACCOUNT_ID", "").strip()
        supplied_token = os.environ.get("AGENT_TOKEN", "").strip()
        if supplied_token:
            agent = self.store.agent(supplied_token)
            if not agent:
                raise ValueError("AGENT_TOKEN is invalid for this PLATFORM_DB. Use a token from this workspace or leave it blank for automatic provisioning.")
            if agent["hosting"] != "self" or (selected and selected != agent["id"]):
                raise ValueError("AGENT_TOKEN must belong to the selected self-hosted account")
            rows = [agent]
        else:
            with self.store.db() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM accounts WHERE hosting='self'")]
        if selected:
            rows = [r for r in rows if r["id"] == selected]
        reason = None
        if not rows:
            reason = "Add a Self-hosted MT5 account in the app; its worker will start automatically."
        elif len(rows) > 1:
            reason = "Multiple self-hosted accounts found. Set LOCAL_ACCOUNT_ID in .env and restart; one terminal supports one account."
        if reason:
            if reason != self.notice:
                log.info(reason)
                self.notice = reason
            return
        account = rows[0]
        if account["heartbeat"] and time.time() - account["heartbeat"] < 60:
            if self.notice != "online":
                log.info("Account has a recent worker heartbeat; waiting without taking it over.")
                self.notice = "online"
            return
        directory = self.state / "workers" / account["id"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        ledger = Path(os.environ.get("WORKER_STATE") or directory / "worker.db").resolve()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        if Path(str(ledger) + ".lock").exists():
            raise ValueError("Worker lock exists. Verify the old worker is stopped before removing its lock: " + str(ledger) + ".lock")
        tokenfile = directory / "agent-token"
        token = supplied_token or (tokenfile.read_text(encoding="utf-8") if tokenfile.exists() else "")
        agent = self.store.agent(token)
        if not agent or agent["id"] != account["id"]:
            token = self.store.rotate_agent(account["user_id"], account["id"])
            # Atomic replacement avoids retaining a partial token after interruption.
            temporary = directory / "agent-token.tmp"
            if temporary.exists():
                temporary.unlink()
            private_file(temporary, token)
            temporary.replace(tokenfile)
            log.info("Local MT5 connection configured. Click Start copying in the app when the account is ready.")
        env = {**os.environ, "AGENT_TOKEN": token, "MT5_TERMINAL_PATH": self.terminal,
               "WORKER_STATE": str(ledger)}
        self.child = start_child("goldcopier.platform_agent", env=env)
        self.account_id = account["id"]
        log.info("Started local MT5 worker for account %s", account["id"])


def main(env_file=".env"):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(env_file)
    os.environ["COPIER_ENV_FILE"] = str(Path(env_file).resolve())
    store, state, base, port, terminal = configure()
    lock = state / "runtime.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"Runner lock exists: {lock}. Verify the old runner is stopped before removing it.") from exc
    children = []
    worker = LocalWorker(store, state, terminal) if terminal else None
    try:
        os.write(fd, str(os.getpid()).encode())
        bootstrap_admin(store)
        bootstrap_analyst(store)
        server = start_child("uvicorn", "goldcopier.platform:create_app", "--factory",
                             "--host", "127.0.0.1", "--port", str(port))
        children.append(server)
        wait_for_server(server, base, os.environ["LOCAL_RUNTIME_ID"])
        log.info("Open %s/#login - Ctrl+C stops the server, listener and local worker.", base)
        if os.environ.get("DISCORD_BOT_TOKEN", "").strip():
            children.append(start_child("goldcopier.discord_platform"))
        else:
            log.info("DISCORD_BOT_TOKEN is empty; Discord listener is disabled.")
        if not worker:
            log.info("MT5_TERMINAL_PATH is empty; local worker is disabled.")
        while True:
            for child in children:
                if child.poll() is not None:
                    raise ValueError("A service stopped unexpectedly. Fix its error above, then restart manage run.")
            if worker:
                worker.tick()
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("Stopping services...")
    finally:
        # Stop execution/intake before the API, preserving worker cleanup.
        stopped = True
        if worker and worker.child:
            stopped = stop_child(worker.child) and stopped
        for child in reversed(children):
            stopped = stop_child(child) and stopped
        os.close(fd)
        if stopped:
            lock.unlink(missing_ok=True)
        else:
            log.error("Runner lock retained because a service may still be running.")
