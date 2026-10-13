"""BalancedBooksRE native Mac entry point, isolated from upstream company files."""

import argparse
import os
from pathlib import Path
import secrets
import sys
import threading
import time


def prepare(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    env = data_dir / "settings.env"
    if not env.exists():
        fd = os.open(env, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(
                "PAYROLL_ENCRYPTION_SECRET="
                + secrets.token_hex(32)
                + "\nSESSION_SECRET_KEY="
                + secrets.token_hex(32)
                + "\n"
            )
    os.environ.update(
        SLOWBOOKS_DATA_DIR=str(data_dir),
        SLOWBOOKS_ENV_FILE=str(env),
        DATABASE_URL="sqlite:///" + str(data_dir / "portfolio.db"),
        APP_DEBUG="true",
        FORCE_HTTPS="false",
        APP_HOST="127.0.0.1",
        SLOWBOOKS_DESKTOP="1",
        APP_VERSION="0.1.0",
    )
    from dotenv import load_dotenv

    load_dotenv(env)
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = os.environ["DATABASE_URL"]
    command.upgrade(cfg, "head")
    ensure_local_token(data_dir)
    return root


def ensure_local_token(data_dir):
    """Authorize the same macOS user's local Codex adapter without exposing a secret."""
    from app.database import SessionLocal
    from app.models.api_tokens import ApiToken
    from app.services.api_token_service import generate_token, hash_token

    path = data_dir / "ai-connection.token"
    with SessionLocal() as db:
        if path.exists():
            os.chmod(path, 0o600)
            # Revocation remains effective: do not silently recreate a revoked token.
            return
        secret = generate_token()
        row = ApiToken(
            label="BalancedBooksRE local Codex",
            token_hash=hash_token(secret),
            token_hint=secret[:10],
            role="bookkeeper",
            is_active=True,
            created_by="local-mac-setup",
        )
        db.add(row)
        db.commit()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secret)


def matching_server(port, data_dir):
    """Only reuse a listener that accepts the token for these exact books."""
    import httpx

    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            result = client.get(
                f"http://127.0.0.1:{port}/api/portfolio/context",
                headers={
                    "Authorization": "Bearer "
                    + (data_dir / "ai-connection.token").read_text().strip()
                },
            )
            return (
                result.status_code == 200
                and result.json().get("product") == "BalancedBooksRE"
            )
    except (httpx.HTTPError, OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(
        description="BalancedBooksRE - private portfolio books"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / "Library/Application Support/BalancedBooksRE",
    )
    parser.add_argument("--port", type=int, default=3188)
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--mcp", action="store_true")
    args = parser.parse_args()
    args.data_dir = args.data_dir.resolve()
    prepare(args.data_dir.resolve())
    os.environ["APP_PORT"] = str(args.port)
    import uvicorn
    from app.main import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info")
    )
    if args.no_window and not args.mcp:
        server.run()
        return 0
    thread = None
    if not matching_server(args.port, args.data_dir):
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(300):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError(
                    "The accounting service could not start. The port may be in use."
                )
            time.sleep(0.1)
        if not server.started:
            raise RuntimeError("The local accounting server did not become ready")
    if args.mcp:
        os.environ["BALANCEDBOOKS_URL"] = f"http://127.0.0.1:{args.port}"
        os.environ["BALANCEDBOOKS_TOKEN_FILE"] = str(
            args.data_dir / "ai-connection.token"
        )
        from app.portfolio.mcp_server import configuration, mcp

        configuration()
        try:
            mcp.run(transport="stdio")
        finally:
            if thread:
                server.should_exit = True
                thread.join(timeout=15)
        return 0
    import webview
    import desktop_launcher as desktop

    desktop.get_data_dir = lambda: args.data_dir.resolve()
    desktop._save_report = lambda name, data, folder="Reports": (
        desktop._write_unique(args.data_dir / "Exports" / folder, name, data),
        None,
    )
    desktop._saved_document_roots = lambda: ((args.data_dir / "Exports").resolve(),)
    bridge = desktop.PickerApi(args.port)
    webview.settings["ALLOW_DOWNLOADS"] = True
    bridge._window = webview.create_window(
        "BalancedBooksRE",
        f"http://127.0.0.1:{args.port}",
        js_api=bridge,
        width=1180,
        height=800,
        min_size=(960, 680),
    )
    try:
        webview.start(private_mode=False, storage_path=str(args.data_dir / "webview"))
    finally:
        if thread:
            server.should_exit = True
            thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback

        log = (
            Path(
                os.environ.get(
                    "SLOWBOOKS_DATA_DIR",
                    str(Path.home() / "Library/Application Support/BalancedBooksRE"),
                )
            )
            / "startup-error.log"
        )
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc())
        raise
