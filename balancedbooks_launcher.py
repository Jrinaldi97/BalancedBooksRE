"""BalancedBooksRE native Mac entry point, isolated from upstream company files."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time


def company_paths(data_dir, filename=None):
    """Keep every company's database and AI credential separate."""
    if filename is None or filename == "portfolio.db":
        return data_dir / "portfolio.db", data_dir / "ai-connection.token", 3188
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.db", filename):
        raise ValueError("Invalid company filename")
    path = data_dir / "companies" / filename
    if not path.is_file() or path.is_symlink():
        raise ValueError("Company file does not exist")
    port = 10000 + int(hashlib.sha256(filename.encode()).hexdigest()[:8], 16) % 40000
    return path, data_dir / "companies" / (filename + ".token"), port


def selected_company(data_dir):
    marker = data_dir / "active-company.json"
    if not marker.exists():
        return "portfolio.db"
    filename = json.loads(marker.read_text())["file"]
    company_paths(data_dir, filename)
    return filename


def prepare(data_dir, filename=None):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    # This serializes setup only. Listener ownership after setup remains a
    # separate lifecycle concern; never hold this lock across server.run().
    with _startup_lock(data_dir):
        return _prepare_locked(data_dir, filename)


@contextmanager
def _startup_lock(data_dir):
    if os.name != "posix":
        raise RuntimeError("Native BalancedBooksRE setup requires macOS/POSIX locking")
    import fcntl

    fd = os.open(
        data_dir / ".native-setup.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Native setup lock must be a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # Releases the lock even after an interrupted setup.


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_create(path, contents):
    """Publish a complete private file without replacing an existing name."""
    fd, temporary = tempfile.mkstemp(prefix=".native-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic publication with no overwrite.
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _private_text(path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Native settings and credential files must be regular files")
    os.chmod(path, 0o600)
    return path.read_text(encoding="utf-8")


def _ensure_settings(env):
    from dotenv import dotenv_values

    if not env.exists():
        _atomic_create(
            env,
            "PAYROLL_ENCRYPTION_SECRET="
            + secrets.token_hex(32)
            + "\nSESSION_SECRET_KEY="
            + secrets.token_hex(32)
            + "\n",
        )
    _private_text(env)
    settings = dotenv_values(env)
    if not all(
        settings.get(key) for key in ("PAYROLL_ENCRYPTION_SECRET", "SESSION_SECRET_KEY")
    ):
        raise RuntimeError(
            "Native settings are incomplete; restore the existing settings.env backup. Secrets were not replaced."
        )


def _prepare_locked(data_dir, filename):
    env = data_dir / "settings.env"
    _ensure_settings(env)
    database, token_path, _ = company_paths(data_dir, filename)
    os.environ.update(
        SLOWBOOKS_DATA_DIR=str(data_dir),
        SLOWBOOKS_ENV_FILE=str(env),
        DATABASE_URL="sqlite:///" + str(database),
        APP_DEBUG="true",
        FORCE_HTTPS="false",
        APP_HOST="127.0.0.1",
        SLOWBOOKS_DESKTOP="1",
        APP_VERSION="0.4.1",
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
    ensure_company_identity()
    _ensure_local_token_locked(token_path)
    return root


def ensure_company_identity():
    """Initialize the existing session identity before either native entry point.

    Token-only companies may never have opened a browser session. Serialize
    first creation so two local launchers cannot assign competing identities.
    Existing companies retain their exact identity and all other settings.
    """
    from sqlalchemy import text
    from app.database import SessionLocal
    from app.models.settings import Settings
    from app.services.auth import COMPANY_SESSION_SETTING

    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.query(Settings).filter_by(key=COMPANY_SESSION_SETTING).first()
        if row is None:
            row = Settings(key=COMPANY_SESSION_SETTING, value=secrets.token_hex(16))
            db.add(row)
        elif not row.value:
            row.value = secrets.token_hex(16)
        identity = row.value
        db.commit()
        return identity


def ensure_local_token(path):
    """Authorize the same macOS user's local Codex adapter without exposing a secret."""
    path = Path(path)
    data_dir = path.parent.parent if path.parent.name == "companies" else path.parent
    with _startup_lock(data_dir):
        _ensure_local_token_locked(path)


def _pending_secret(path):
    try:
        pending = json.loads(_private_text(path))
        secret = pending["secret"]
        if pending["version"] != 1 or not re.fullmatch(
            r"sbp_[A-Za-z0-9_-]{43}", secret
        ):
            raise ValueError("Invalid issuance record")
        return secret
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            "Interrupted native credential issuance is invalid; restore or review its pending file. No token was replaced."
        ) from error


def _remove_pending(path):
    path.unlink()
    _sync_directory(path.parent)


def _ensure_local_token_locked(path):
    from app.database import SessionLocal
    from app.models.api_tokens import ApiToken
    from app.services.api_token_service import generate_token, hash_token
    from app.services.audit import register_audit_hooks

    pending = path.with_name(path.name + ".pending")
    if path.exists():
        secret = _private_text(path).strip()
        if not re.fullmatch(r"sbp_[A-Za-z0-9_-]{43}", secret):
            raise RuntimeError(
                "Existing native credential is incomplete; restore its backup. No token was replaced."
            )
        if pending.exists():
            if _pending_secret(pending) != secret:
                raise RuntimeError(
                    "Pending issuance conflicts with the existing native credential. No token was replaced."
                )
            _remove_pending(pending)
        # An existing secret is never rotated or reactivated, including after
        # revocation. Preserve its exact bytes (even a trailing newline).
        return
    register_audit_hooks(SessionLocal)
    with SessionLocal() as db:
        label = "BalancedBooksRE local Codex"
        row = db.query(ApiToken).filter_by(label=label).first()
        if not pending.exists():
            if row is not None:
                raise RuntimeError(
                    "The native token exists in this company's database but its secret is missing. Restore the credential backup or rotate it explicitly; startup will not replace it."
                )
            # Durable intent precedes the database commit. A crash at either
            # side of commit can resume only this exact newly generated secret.
            _atomic_create(
                pending, json.dumps({"version": 1, "secret": generate_token()})
            )
        secret = _pending_secret(pending)
        token_hash = hash_token(secret)
        matching_hash = db.query(ApiToken).filter_by(token_hash=token_hash).first()
        if row is not None:
            if row.token_hash != token_hash or not row.is_active:
                raise RuntimeError(
                    "Pending native issuance conflicts with an existing or revoked token. No token was changed."
                )
            # A lost commit acknowledgment is recoverable. Preserve all current
            # role/metadata; an administrator may have changed them since commit.
        elif matching_hash is not None:
            raise RuntimeError(
                "Pending native issuance belongs to another token. No token was changed."
            )
        else:
            db.add(
                ApiToken(
                    label=label,
                    token_hash=token_hash,
                    token_hint=secret[:10],
                    role="bookkeeper",
                    is_active=True,
                    created_by="local-mac-setup",
                )
            )
            db.commit()
        _atomic_create(path, secret)
        _remove_pending(pending)


def matching_server(port, token_path):
    """Only reuse a listener that accepts the token for these exact books."""
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "GET",
            "/api/portfolio/context",
            headers={"Authorization": "Bearer " + token_path.read_text().strip()},
        )
        response = connection.getresponse()
        return (
            response.status == 200
            and json.loads(response.read()).get("product") == "BalancedBooksRE"
        )
    except (http.client.HTTPException, OSError, ValueError):
        return False
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(
        description="BalancedBooksRE - private portfolio books"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / "Library/Application Support/BalancedBooksRE",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("--company-file")
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--mcp", action="store_true")
    args = parser.parse_args()
    args.data_dir = args.data_dir.resolve()
    filename = args.company_file or selected_company(args.data_dir)
    _, token_path, default_port = company_paths(args.data_dir, filename)
    args.port = args.port or default_port
    prepare(args.data_dir.resolve(), filename)
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
    if not matching_server(args.port, token_path):
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
        os.environ["BALANCEDBOOKS_TOKEN_FILE"] = str(token_path)
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
    desktop.PICKER_HTML = desktop.PICKER_HTML.replace(
        "SlowBooks Pro 2026", "BalancedBooksRE"
    )

    class CompanyPicker(desktop.PickerApi):
        def list_companies(self):
            result = super().list_companies()
            original = (
                [{"name": "Original portfolio", "file": "portfolio.db"}]
                if (args.data_dir / "portfolio.db").is_file()
                else []
            )
            result["companies"] = original + [
                company
                for company in result["companies"]
                if (args.data_dir / "companies" / company["file"]).is_file()
            ]
            result["last_opened"] = selected_company(args.data_dir)
            return result

        def open_company(self, filename):
            try:
                _, credential, port = company_paths(args.data_dir, filename)
                if not matching_server(port, credential):
                    if desktop._server_already_running(port):
                        return {
                            "success": False,
                            "error": "Another service uses this company's port. No books were switched.",
                        }
                    command = [sys.executable]
                    if not getattr(sys, "frozen", False):
                        command.append(str(Path(__file__).resolve()))
                    command += [
                        "--data-dir",
                        str(args.data_dir),
                        "--company-file",
                        filename,
                        "--port",
                        str(port),
                        "--no-window",
                    ]
                    log = open(args.data_dir / "company-server.log", "a")
                    try:
                        process = subprocess.Popen(
                            command, stdout=log, stderr=subprocess.STDOUT
                        )
                    finally:
                        log.close()
                    if not desktop.wait_for_health(
                        process, port, timeout=30
                    ) or not matching_server(port, credential):
                        desktop.stop_server(process)
                        return {
                            "success": False,
                            "error": "The company could not open. See company-server.log.",
                        }
                    self.children.append(process)
                marker = args.data_dir / "active-company.json"
                pending = marker.with_suffix(".tmp")
                pending.write_text(json.dumps({"file": filename}))
                pending.replace(marker)
                return {"success": True, "url": f"http://127.0.0.1:{port}"}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

    bridge = CompanyPicker(args.port)
    bridge.children = []
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
        for child in bridge.children:
            desktop.stop_server(child)
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
