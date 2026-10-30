"""Single-machine server, Discord listener and dedicated local MT5 worker."""

import importlib.util
import json
import logging
import os
from pathlib import Path
import re
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
from .platform_models import AccountInput, Analyst, Signup
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


def process_alive(pid):
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def clear_stale_lock(path):
    """Remove a lock whose recorded process has exited; keep any other lock."""
    try:
        pid = int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid() or process_alive(pid):
        return False
    Path(path).unlink(missing_ok=True)
    log.info("Removed stale lock from stopped process %s: %s", pid, path)
    return True


def bind_children_to_runner():
    """Windows: services share a kill-on-close job with the runner, so they can
    never outlive it, however the runner ends (closed window, repeated Ctrl+C)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (job and kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        log.warning("Could not tie services to this window; if it is closed, stop leftover python processes manually.")
        return None
    return job  # The handle must stay open for the runner's lifetime.


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
    terminal = find_terminal()
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


def find_terminal():
    """MT5_TERMINAL_PATH, else the only terminal64.exe under Program Files."""
    terminal = os.environ.get("MT5_TERMINAL_PATH", "").strip()
    if terminal or sys.platform != "win32":
        return terminal
    roots = {os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)")}
    found = sorted({str(p) for root in roots if root for p in Path(root).glob("*/terminal64.exe")})
    if len(found) > 1:
        raise ValueError("Several MT5 terminals found. Set MT5_TERMINAL_PATH in .env to one of: " + "; ".join(found))
    if found:
        log.info("Using MT5 terminal %s", found[0])
    return found[0] if found else ""


def bootstrap_admin(store, state):
    with store.db() as db:
        if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            return
    email = os.environ.get("ADMIN_EMAIL", "").strip() or "admin@goldcopier.local"
    password = os.environ.get("ADMIN_PASSWORD", "")
    passfile = state / "admin-password"
    if not password:
        if not passfile.exists():
            private_file(passfile, secrets.token_urlsafe(18))
        password = passfile.read_text(encoding="utf-8").strip()
    try:
        body = Signup(name=os.environ.get("ADMIN_NAME") or "Local operator", email=email, password=password)
    except ValidationError as exc:
        # ValidationError's default string includes submitted values/passwords.
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors())
        raise ValueError(f"Invalid operator {fields} in .env. Use a valid email, a name of 2-80 characters and a password of 12-128 characters.") from None
    with store.db() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (body.email.lower().strip(),)).fetchone():
            raise ValueError("That email already exists. Create an operator using manage create-admin with a different email.")
    store.signup(body.name, body.email, body.password, "admin")
    where = "ADMIN_PASSWORD from .env" if os.environ.get("ADMIN_PASSWORD") else f"the password saved in {passfile}"
    log.info("Operator account created. Sign in as %s with %s.", body.email, where)


def discord_guild(channel_id):
    """Resolve a channel's server ID with the bot token, so .env needs no guild ID."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Set DISCORD_BOT_TOKEN (or DISCORD_GUILD_ID) so the signal channel's server can be found")
    try:
        response = httpx.get(f"https://discord.com/api/v10/channels/{channel_id}",
                             headers={"Authorization": "Bot " + token}, timeout=10)
    except httpx.HTTPError as exc:
        raise ValueError("Could not reach Discord to look up DISCORD_CHANNEL_ID; check your connection or set DISCORD_GUILD_ID") from exc
    if response.status_code == 401:
        raise ValueError("Discord rejected DISCORD_BOT_TOKEN")
    if response.status_code != 200 or not response.json().get("guild_id"):
        raise ValueError("The bot cannot see DISCORD_CHANNEL_ID. Invite the bot to that server and give it access to the channel.")
    return str(response.json()["guild_id"])


def bootstrap_analyst(store):
    fields = {key: os.environ.get("DISCORD_" + key.upper(), "").strip()
              for key in ("guild_id", "channel_id", "trader_id")}
    if not any(fields.values()):
        return
    if not (fields["channel_id"].isdigit() and fields["trader_id"].isdigit()):
        raise ValueError("Set numeric DISCORD_CHANNEL_ID and DISCORD_TRADER_ID in .env")
    query = "SELECT id FROM analysts WHERE channel_id=? AND trader_id=?"
    params = [fields["channel_id"], fields["trader_id"]]
    if fields["guild_id"]:
        query, params = query + " AND guild_id=?", params + [fields["guild_id"]]
    with store.db() as db:
        existing = db.execute(query, params).fetchall()
    if len(existing) > 1:
        raise ValueError("Duplicate analyst sources exist. Keep one configured analyst for these Discord IDs.")
    if existing:
        return existing[0]["id"]
    fields["guild_id"] = fields["guild_id"] or discord_guild(fields["channel_id"])
    try:
        analyst = Analyst(name=os.environ.get("ANALYST_NAME") or "My signal feed", **fields)
    except ValidationError:
        raise ValueError("Discord IDs in .env must be numeric") from None
    return store.add_analyst(analyst.model_dump())


def read_terminal(terminal):
    """Login, server, mode and gold symbol of the account open in the MT5 terminal."""
    import MetaTrader5 as mt

    options = {"path": terminal}
    login, server = os.environ.get("MT5_LOGIN", "").strip(), os.environ.get("MT5_SERVER", "").strip()
    password = os.environ.get("MT5_PASSWORD", "")
    if login and server and password:
        if not login.isdigit():
            raise ValueError("MT5_LOGIN must be your numeric MT5 account number")
        options.update(login=int(login), server=server, password=password)
    hint = ("Log in to your demo account in MetaTrader 5 once (File > Login to Trade Account, tick "
            "Save password), or set MT5_LOGIN, MT5_SERVER and MT5_PASSWORD in .env.")
    if not mt.initialize(**options):
        raise ValueError(f"MT5 terminal connection failed: {mt.last_error()}. {hint}")
    try:
        info = mt.account_info()
        if info is None or not info.login:
            raise ValueError("MT5 is not logged in to a trading account. " + hint)
        names = [s.name for s in mt.symbols_get("XAUUSD*") or ()]
        demo = info.trade_mode == mt.ACCOUNT_TRADE_MODE_DEMO
    finally:
        mt.shutdown()
    symbol = os.environ.get("MT5_SYMBOL", "").strip()
    if not symbol:
        gold = sorted((n for n in names if re.fullmatch(r"XAUUSD[A-Za-z0-9._-]{0,12}", n)), key=len)
        if not gold:
            raise ValueError("No XAUUSD symbol found in MT5. Set MT5_SYMBOL in .env to your broker's gold symbol.")
        symbol = gold[0]
    return {"login": str(info.login), "server": info.server, "symbol": symbol,
            "mode": "demo" if demo else "live"}


def bootstrap_account(store, terminal, analyst_id=None):
    """Register the terminal's account on first start so the worker token can be
    provisioned before anyone signs in. An existing self-hosted account is only made
    to follow the .env analyst, e.g. after DISCORD_TRADER_ID changes."""
    if not terminal:
        return
    with store.db() as db:
        existing = db.execute("SELECT id,user_id FROM accounts WHERE hosting='self'").fetchall()
    if existing:
        if analyst_id and len(existing) == 1:
            with store.db() as db:
                followed = db.execute("SELECT 1 FROM follows WHERE account_id=? AND analyst_id=?",
                                      (existing[0]["id"], analyst_id)).fetchone()
            if not followed:
                store.follow(existing[0]["user_id"], existing[0]["id"], analyst_id, True)
                log.info("MT5 account now follows the analyst from .env. Unfollow old analysts in the app if needed.")
        return
    with store.db() as db:
        owner = db.execute("SELECT id FROM users WHERE role='admin' ORDER BY created LIMIT 1").fetchone()[0]
        if not analyst_id:
            rows = db.execute("SELECT id FROM analysts WHERE active=1").fetchall()
            analyst_id = rows[0]["id"] if len(rows) == 1 else None
    found = read_terminal(terminal)
    if found["mode"] == "live" and os.environ.get("ALLOW_LIVE_TRADING", "false") != "true":
        raise ValueError("MT5 is logged in to a LIVE account. Log in to a demo account; live copying is disabled.")
    server = found["server"].lower()
    broker = "Exness" if "exness" in server else "PrimeXBT" if "primexbt" in server else "Other MT5"
    try:
        body = AccountInput(name="Local MT5", broker=broker, hosting="self", consent=True,
                            analyst_id=analyst_id, **found)
    except ValidationError:
        raise ValueError(f"MT5 account details are not supported (server {found['server']!r}, symbol {found['symbol']!r})") from None
    data = body.model_dump(exclude={"consent"})
    store.add_account(owner, data, Vault(os.environ["VAULT_KEY"]))
    log.info("Registered MT5 %s account %s on %s (%s).", found["mode"], found["login"], found["server"], found["symbol"])


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
        if Path(str(ledger) + ".lock").exists() and not clear_stale_lock(str(ledger) + ".lock"):
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
    if lock.exists():
        clear_stale_lock(lock)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"Runner lock exists: {lock}. Verify the old runner is stopped before removing it.") from exc
    children = []
    worker = LocalWorker(store, state, terminal) if terminal else None
    job = bind_children_to_runner()  # noqa: F841 - kept open until exit
    try:
        os.write(fd, str(os.getpid()).encode())
        bootstrap_admin(store, state)
        bootstrap_account(store, terminal, bootstrap_analyst(store))
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
            log.info("No MT5 terminal found; local worker is disabled. Set MT5_TERMINAL_PATH in .env.")
        while True:
            for child in children:
                if child.poll() is not None:
                    raise ValueError("A service stopped unexpectedly. Fix its error above, then restart manage run.")
            if worker:
                worker.tick()
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("Stopping services, please wait (up to 20 seconds)...")
    finally:
        # A second Ctrl+C must not abort cleanup and orphan the services.
        interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)
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
        signal.signal(signal.SIGINT, interrupt)
