import os
import socket
import time
from types import SimpleNamespace as Obj

import httpx
import pytest

from goldcopier import runtime
from goldcopier.auth import Vault
from goldcopier.platform_store import PlatformStore


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # configure() updates the environment directly; isolate all those writes too.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in (
        "PUBLIC_URL", "COPIER_URL", "PLATFORM_DB", "VAULT_KEY", "DEMO_MODE",
        "LOCAL_STATE_DIR", "LOCAL_ACCOUNT_ID", "LOCAL_RUNTIME_ID",
        "MT5_TERMINAL_PATH", "DISCORD_BOT_TOKEN", "ALLOW_LIVE_TRADING",
        "BILLING_ENABLED", "AGENT_TOKEN", "WORKER_STATE", "COPIER_ENV_FILE",
        "ADMIN_EMAIL", "ADMIN_PASSWORD", "ADMIN_NAME", "MT5_PASSWORD",
        "DISCORD_GUILD_ID", "DISCORD_CHANNEL_ID", "DISCORD_TRADER_ID", "ANALYST_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("PUBLIC_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("LOCAL_STATE_DIR", str(tmp_path / "runtime"))
    return tmp_path


def account(store, login="123", **kwargs):
    with store.db() as db:
        row = db.execute("SELECT id FROM users LIMIT 1").fetchone()
    owner = row[0] if row else store.signup("Owner", "owner@example.com", "long-test-password", "admin")
    ident = store.add_account(owner, {
        "name": "Local MT5", "broker": "Exness", "login": login,
        "server": "Demo", "symbol": "XAUUSD", "mode": "demo",
        "hosting": "self", "password": "", **kwargs,
    }, Vault(os.environ["VAULT_KEY"]))
    return owner, ident


def test_literal_env_preserves_paths_and_shell_values(tmp_path):
    file = tmp_path / ".env"
    file.write_text('\ufeff# config\nMT5_TERMINAL_PATH="C:\\Program Files\\MT5\\terminal64.exe" # note\n'
                    "export DISCORD_BOT_TOKEN='literal$token#value'\nPUBLIC_URL=file\nEMPTY=\n", encoding="utf-8")
    env = {"PUBLIC_URL": "shell"}
    runtime.load_env(file, env)
    assert env == {
        "PUBLIC_URL": "shell", "EMPTY": "",
        "MT5_TERMINAL_PATH": r"C:\Program Files\MT5\terminal64.exe",
        "DISCORD_BOT_TOKEN": "literal$token#value",
    }


@pytest.mark.parametrize("line", ["not a setting", "TOKEN='secret", "TOKEN='secret' invalid"])
def test_bad_env_never_prints_secret(tmp_path, line):
    file = tmp_path / ".env"
    file.write_text(line)
    with pytest.raises(ValueError) as error:
        runtime.load_env(file, {})
    assert "secret" not in str(error.value)


def test_configuration_keeps_key_and_database_across_restarts(settings):
    store, state, base, port, terminal = runtime.configure()
    key = os.environ["VAULT_KEY"]
    owner, ident = account(store, hosting="managed", password="broker-secret")
    os.environ.pop("VAULT_KEY")
    again, _, _, _, _ = runtime.configure()
    assert os.environ["VAULT_KEY"] == key
    assert again.account(owner, ident)["secret"]
    assert (state / "vault-key").read_text() == key
    assert os.environ["ALLOW_LIVE_TRADING"] == "false"
    assert os.environ["DEMO_MODE"] == "false"
    assert terminal == ""


def test_missing_original_key_is_never_replaced(settings):
    store, state, *_ = runtime.configure()
    account(store, hosting="managed", password="broker-secret")
    (state / "vault-key").unlink()
    os.environ.pop("VAULT_KEY")
    with pytest.raises(ValueError, match="original VAULT_KEY"):
        runtime.configure()
    assert not (state / "vault-key").exists()


@pytest.mark.parametrize("demo", ["true", "TRUE"])
def test_seeded_mode_is_rejected(settings, monkeypatch, demo):
    monkeypatch.setenv("DEMO_MODE", demo)
    with pytest.raises(ValueError, match="DEMO_MODE=false"):
        runtime.configure()


def test_seeded_database_is_rejected_even_with_demo_flag_off(settings):
    store, *_ = runtime.configure()
    store.signup("Demo", "demo@goldcopier.local", "long-demo-password")
    with pytest.raises(ValueError, match="seeded preview database"):
        runtime.configure()


def test_occupied_port_is_rejected(settings, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        monkeypatch.setenv("PUBLIC_URL", f"http://127.0.0.1:{sock.getsockname()[1]}")
        with pytest.raises(ValueError, match="already in use"):
            runtime.configure()


def test_worker_waits_for_account_and_provisions_once(settings, monkeypatch):
    store, state, *_ = runtime.configure()
    calls = []
    child = Obj(poll=lambda: None)
    monkeypatch.setattr(runtime, "start_child", lambda module, **kw: calls.append((module, kw)) or child)
    worker = runtime.LocalWorker(store, state, "terminal.exe")
    worker.tick()
    assert not calls
    owner, ident = account(store)
    worker.tick()
    worker.tick()
    assert len(calls) == 1
    module, options = calls[0]
    assert module == "goldcopier.platform_agent"
    token = options["env"]["AGENT_TOKEN"]
    assert store.agent(token)["id"] == ident
    assert store.account(owner, ident)["enabled"] == 0
    assert str(state) in options["env"]["WORKER_STATE"]
    # Reusing this valid token on a subsequent run must not pause copying again.
    with store.db() as db:
        db.execute("UPDATE accounts SET enabled=1 WHERE id=?", (ident,))
    again = runtime.LocalWorker(store, state, "terminal.exe")
    again.tick()
    assert calls[-1][1]["env"]["AGENT_TOKEN"] == token
    assert store.account(owner, ident)["enabled"] == 1


def test_worker_never_guesses_among_multiple_accounts(settings, monkeypatch):
    store, state, *_ = runtime.configure()
    account(store)
    _, selected = account(store, login="456")
    calls = []
    monkeypatch.setattr(runtime, "start_child", lambda *args, **kw: calls.append(kw) or Obj(poll=lambda: None))
    worker = runtime.LocalWorker(store, state, "terminal.exe")
    worker.tick()
    assert not calls
    monkeypatch.setenv("LOCAL_ACCOUNT_ID", selected)
    worker.tick()
    assert store.agent(calls[0]["env"]["AGENT_TOKEN"])["id"] == selected


def test_supplied_token_selects_account_and_preserves_existing_journal(settings, monkeypatch):
    store, state, *_ = runtime.configure()
    account(store)
    owner, ident = account(store, login="456")
    token = store.rotate_agent(owner, ident)
    journal = settings / "existing-worker.db"
    journal.write_bytes(b"existing journal")
    with store.db() as db:
        db.execute("UPDATE accounts SET enabled=1 WHERE id=?", (ident,))
    monkeypatch.setenv("AGENT_TOKEN", token)
    monkeypatch.setenv("WORKER_STATE", str(journal))
    calls = []
    monkeypatch.setattr(runtime, "start_child", lambda *a, **kw: calls.append(kw) or Obj(poll=lambda: None))
    monkeypatch.setattr(store, "rotate_agent", lambda *a: pytest.fail("must reuse supplied token"))
    runtime.LocalWorker(store, state, "terminal.exe").tick()
    assert calls[0]["env"]["AGENT_TOKEN"] == token
    assert calls[0]["env"]["WORKER_STATE"] == str(journal)
    assert journal.read_bytes() == b"existing journal"
    assert store.account(owner, ident)["enabled"] == 1
    assert not (state / "workers" / ident / "agent-token").exists()


@pytest.mark.parametrize("problem", ["invalid", "mismatch", "managed"])
def test_invalid_explicit_token_never_falls_back_to_provisioning(settings, monkeypatch, problem):
    store, state, *_ = runtime.configure()
    owner, ident = account(store, hosting="managed" if problem == "managed" else "self",
                           password="broker-password" if problem == "managed" else "")
    token = store.rotate_agent(owner, ident)
    monkeypatch.setenv("AGENT_TOKEN", "invalid-token" if problem == "invalid" else token)
    if problem == "mismatch":
        monkeypatch.setenv("LOCAL_ACCOUNT_ID", "another-account")
    monkeypatch.setattr(store, "rotate_agent", lambda *a: pytest.fail("must not provision"))
    monkeypatch.setattr(runtime, "start_child", lambda *a, **kw: pytest.fail("must not start"))
    with pytest.raises(ValueError, match="AGENT_TOKEN"):
        runtime.LocalWorker(store, state, "terminal.exe").tick()


def test_runner_rejects_worker_url_pointing_to_another_server(settings, monkeypatch):
    monkeypatch.setenv("COPIER_URL", "http://127.0.0.1:8000")
    with pytest.raises(ValueError, match="COPIER_URL must match PUBLIC_URL"):
        runtime.configure()


def test_standalone_worker_reads_env_without_powershell_assignments(settings, monkeypatch):
    from goldcopier import platform_agent

    monkeypatch.chdir(settings)
    (settings / ".env").write_text(
        'COPIER_URL=http://127.0.0.1:8000/\nAGENT_TOKEN=test-worker-token\n'
        'MT5_TERMINAL_PATH="C:\\Program Files\\MetaTrader 5\\terminal64.exe"\n'
        'MT5_FILLING_MODE=1\nWORKER_STATE=state/worker.db\n'
    )
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, path):
            assert path == "/api/worker/config"
            # Stop before MT5 initialization; this test cannot send orders.
            return Obj(raise_for_status=lambda: None, json=lambda: {"demo_workspace": True})

    monkeypatch.setattr(platform_agent.httpx, "Client", Client)
    monkeypatch.setattr(platform_agent, "Ledger", lambda path: Obj(recover=lambda: None))
    with pytest.raises(ValueError, match="Seeded demo workspaces"):
        platform_agent.main()
    assert calls[0]["base_url"] == "http://127.0.0.1:8000"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-worker-token"
    assert os.environ["MT5_TERMINAL_PATH"] == r"C:\Program Files\MetaTrader 5\terminal64.exe"
    assert os.environ["WORKER_STATE"] == "state/worker.db"
    assert not (settings / "state" / "worker.db.lock").exists()


@pytest.mark.parametrize("blocked", ["heartbeat", "lock"])
def test_worker_does_not_take_over_existing_execution(settings, monkeypatch, blocked):
    store, state, *_ = runtime.configure()
    _, ident = account(store)
    monkeypatch.setattr(runtime, "start_child", lambda *a, **kw: pytest.fail("must not start"))
    worker = runtime.LocalWorker(store, state, "terminal.exe")
    if blocked == "heartbeat":
        with store.db() as db:
            db.execute("UPDATE accounts SET heartbeat=? WHERE id=?", (time.time(), ident))
        worker.tick()
    else:
        folder = state / "workers" / ident
        folder.mkdir(parents=True)
        (folder / "worker.db.lock").touch()
        with pytest.raises(ValueError, match="Worker lock exists"):
            worker.tick()


def test_failed_worker_is_not_restarted(settings, monkeypatch):
    store, state, *_ = runtime.configure()
    owner, ident = account(store)
    worker = runtime.LocalWorker(store, state, "terminal.exe")
    worker.account_id = ident
    worker.child = Obj(poll=lambda: 1)
    monkeypatch.setattr(runtime, "start_child", lambda *a, **kw: pytest.fail("must not restart"))
    with store.db() as db:
        db.execute("UPDATE accounts SET enabled=1,heartbeat=? WHERE id=?", (time.time(), ident))
    entry = store.control(owner, ident, {"action": "entry"})
    management = store.control(owner, ident, {"action": "close", "position": 42})
    worker.tick()
    worker.tick()
    result = store.dashboard(owner)
    assert result["accounts"][0]["enabled"] == 0
    assert result["accounts"][0]["online"] is False
    assert "MT5 connection stopped" in result["accounts"][0]["snapshot"]["error"]
    states = {job["id"]: job["state"] for job in result["jobs"]}
    assert states[entry] == "cancelled"
    assert states[management] == "queued"
    assert len([e for e in result["events"] if e["kind"] == "alert"]) == 1


def test_runner_failure_stops_every_started_service(settings, monkeypatch):
    store, state, base, port, terminal = runtime.configure()
    store.signup("Owner", "owner@example.com", "long-test-password", "admin")
    server = Obj(poll=lambda: None)
    listener = Obj(poll=lambda: 1)
    children = iter([server, listener])
    stopped = []
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setattr(runtime, "configure", lambda: (store, state, base, port, terminal))
    monkeypatch.setattr(runtime, "load_env", lambda _: None)
    monkeypatch.setattr(runtime, "start_child", lambda *a, **kw: next(children))
    monkeypatch.setattr(runtime, "wait_for_server", lambda *a: None)
    monkeypatch.setattr(runtime, "stop_child", lambda child: stopped.append(child) or True)
    with pytest.raises(ValueError, match="service stopped unexpectedly"):
        runtime.main()
    assert stopped == [listener, server]
    assert not (state / "runtime.lock").exists()


def test_bootstrap_creates_operator_only_once(settings, monkeypatch):
    store, *_ = runtime.configure()
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "long-test-password")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    runtime.bootstrap_admin(store)
    monkeypatch.setenv("ADMIN_PASSWORD", "different-long-password")
    runtime.bootstrap_admin(store)
    assert store.login("owner@example.com", "long-test-password")[2]["role"] == "admin"
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] == 1


def test_bootstrap_validation_does_not_expose_password(settings, monkeypatch):
    store, *_ = runtime.configure()
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    with pytest.raises(ValueError) as error:
        runtime.bootstrap_admin(store)
    assert "secret" not in str(error.value)


def test_bootstrap_missing_credentials_has_actionable_error(settings):
    store, *_ = runtime.configure()
    with pytest.raises(ValueError, match="ADMIN_EMAIL and ADMIN_PASSWORD"):
        runtime.bootstrap_admin(store)


def test_env_analyst_is_created_once_and_never_silently_reenabled(settings, monkeypatch):
    store, *_ = runtime.configure()
    monkeypatch.setenv("DISCORD_GUILD_ID", "100")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "200")
    monkeypatch.setenv("DISCORD_TRADER_ID", "300")
    ident = runtime.bootstrap_analyst(store)
    with store.db() as db:
        db.execute("UPDATE analysts SET active=0 WHERE id=?", (ident,))
    assert runtime.bootstrap_analyst(store) == ident
    with store.db() as db:
        rows = db.execute("SELECT * FROM analysts").fetchall()
        assert len(rows) == 1 and rows[0]["active"] == 0


def test_incomplete_env_analyst_is_rejected(settings, monkeypatch):
    store, *_ = runtime.configure()
    monkeypatch.setenv("DISCORD_GUILD_ID", "100")
    with pytest.raises(ValueError, match="all three numeric Discord IDs"):
        runtime.bootstrap_analyst(store)


def test_full_runner_serves_its_database_and_stops_cleanly(settings, tmp_path):
    store, state, base, *_ = runtime.configure()
    envfile = tmp_path / ".env"
    envfile.write_text("ADMIN_EMAIL=owner@example.com\nADMIN_PASSWORD=long-test-password\n"
                       "DISCORD_GUILD_ID=100\nDISCORD_CHANNEL_ID=200\nDISCORD_TRADER_ID=300\n")
    # Real subprocess smoke test: no Discord credentials, no MT5 terminal/orders.
    child = runtime.start_child("goldcopier.manage", "run", "--env-file", str(envfile))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert child.poll() is None
            try:
                with httpx.Client(base_url=base, timeout=1, trust_env=False) as client:
                    r = client.post("/api/auth/login", json={
                        "email": "owner@example.com", "password": "long-test-password",
                    })
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        else:
            pytest.fail("runner did not serve its configured database")
        assert (state / "runtime.lock").exists()
        with store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM analysts").fetchone()[0] == 1
    finally:
        assert runtime.stop_child(child)
    assert not (state / "runtime.lock").exists()
