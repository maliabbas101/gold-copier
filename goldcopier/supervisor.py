"""Managed Windows terminal pool. One supervisor, dedicated MT5 install per slot.

No automatic failover: a replacement requires a verified stop of the old pool.
Secrets are passed to child workers by environment and never written into slot files.
"""

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx


def main():
    logging.basicConfig(level=logging.INFO)
    url = os.environ["COPIER_URL"].rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    ):
        raise ValueError("HTTPS required outside localhost")
    pool = Path(os.environ.get("TERMINAL_POOL", "terminal-pool.json"))
    slots = json.loads(pool.read_text())["terminals"]
    if len(set(slots)) != len(slots):
        raise ValueError("Terminal slots must use distinct installations")
    lock = pool.with_suffix(".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    state = Path(os.environ.get("POOL_STATE", "pool-state"))
    state.mkdir(parents=True, exist_ok=True)
    children = {}
    # Preserve account-to-terminal binding across restarts. Credentials/tokens stay out of this file.
    binding_file = state / "bindings.json"
    bindings = json.loads(binding_file.read_text()) if binding_file.exists() else {}
    try:
        with httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]},
            timeout=15,
        ) as client:
            while True:
                try:
                    response = client.get("/api/supervisor/assignments")
                    response.raise_for_status()
                    assignments = response.json()
                    active = {a["id"] for a in assignments}
                    for ident, child in list(children.items()):
                        if ident not in active:
                            child.terminate()
                            child.wait(timeout=20)
                            children.pop(ident)
                        elif child.poll() is not None:
                            logging.error(
                                "Worker %s stopped; operator must reconcile before restarting supervisor",
                                ident,
                            )
                            # Keep record to avoid a tight restart/credential-rotation loop.
                    for a in assignments:
                        if a["id"] in children:
                            continue
                        # Never steal an apparently running assignment, even after a supervisor restart.
                        if a["heartbeat"] and time.time() - a["heartbeat"] < 60:
                            continue
                        slot = bindings.get(a["id"])
                        occupied = set(bindings.values())
                        if slot is None:
                            slot = next((s for s in slots if s not in occupied), None)
                        if slot is None:
                            logging.warning(
                                "No free terminal slot for account %s", a["id"]
                            )
                            continue
                        directory = state / a["id"]
                        directory.mkdir(exist_ok=True)
                        if (directory / "platform-worker.db.lock").exists():
                            logging.error(
                                "Stale worker lock for %s; manual verification required",
                                a["id"],
                            )
                            continue
                        response = client.post(
                            f"/api/supervisor/accounts/{a['id']}/token"
                        )
                        response.raise_for_status()
                        token = response.json()["token"]
                        env = {
                            **os.environ,
                            "AGENT_TOKEN": token,
                            "MT5_TERMINAL_PATH": slot,
                            "WORKER_STATE": "platform-worker.db",
                        }
                        # Pool and per-worker logs are operator-managed; never print credentials.
                        children[a["id"]] = subprocess.Popen(
                            [sys.executable, "-m", "goldcopier.platform_agent"],
                            cwd=directory,
                            env=env,
                        )
                        bindings[a["id"]] = slot
                        temporary = binding_file.with_suffix(".tmp")
                        temporary.write_text(json.dumps(bindings))
                        temporary.replace(binding_file)
                        logging.info(
                            "Started dedicated terminal worker for %s", a["id"]
                        )
                except (httpx.HTTPError, ValueError):
                    logging.exception("Pool synchronization failed")
                time.sleep(10)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                logging.error("Worker still running; verify before pool restart")
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
