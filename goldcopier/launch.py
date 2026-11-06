"""One-command, localhost-only demo launcher. Production uses the deployment guide."""

import argparse
import base64
import os
from pathlib import Path
import secrets


def prepare_demo(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    key = directory / "vault-key"
    password = directory / "access-password"
    for file, value in [
        (key, base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()),
        (password, os.environ.get("DEMO_PASSWORD") or secrets.token_urlsafe(24)),
    ]:
        if not file.exists():
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(value)
    os.environ.update(
        DEMO_MODE="true",
        ALLOW_LIVE_TRADING="false",
        BILLING_ENABLED="false",
        PLATFORM_DB=str(directory / "platform.db"),
        VAULT_KEY=key.read_text(),
    )
    from .platform_store import PlatformStore
    from .manage import seed_demo

    store = PlatformStore(os.environ["PLATFORM_DB"])
    with store.db() as db:
        populated = bool(db.execute("SELECT 1 FROM users").fetchone())
    if not populated:
        seed_demo(store, password.read_text())
    print("Read-only preview: http://127.0.0.1:8000/#demo")
    print(
        "Optional operator sign-in: demo@goldcopier.local; password is in "
        + str(password)
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--state-dir", default=".demo")
    args = parser.parse_args()
    if not args.demo:
        raise ValueError(
            "Use --demo for local preview; use the deployment guide for production"
        )
    os.environ["PUBLIC_URL"] = "http://127.0.0.1:8000"
    prepare_demo(args.state_dir)
    import uvicorn

    uvicorn.run(
        "goldcopier.platform:create_app", factory=True, host="127.0.0.1", port=8000
    )


if __name__ == "__main__":
    main()
