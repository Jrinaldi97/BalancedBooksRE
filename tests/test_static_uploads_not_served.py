"""/static never serves the uploads folder, however the path is spelled.

Before 2.18 every company's logo, W-4s and attachments were files under
app/static/uploads, and /static needs no sign-in. A route answering 404 for
the literal /static/uploads/ prefix was registered before the /static mount,
but StaticFiles resolves the request path itself: /static//uploads/<file> and
/static/css/%2e%2e/uploads/<file> never match that literal route and reached
the files with 200 (reproduced on Starlette 1.9.0 via TestClient). The mount
now refuses anything that resolves inside <static>/uploads, whatever spelling
got it there; this pins the bypasses shut and keeps ordinary assets served.
"""

import pytest

from app.main import static_dir

# A W-4 as a pre-2.18 install left it — the kind of file that must never be
# reachable over /static without a sign-in.
SECRET = b"%PDF-1.4 W-4 Marisol Vance - must never be served over /static"


@pytest.fixture
def uploads_file():
    """A document in app/static/uploads, cleaned up afterwards (and the
    folder too, but only when this fixture is the one that created it)."""
    uploads = static_dir / "uploads"
    created_dir = not uploads.exists()
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / "w4.pdf"
    path.write_bytes(SECRET)
    try:
        yield path
    finally:
        if path.exists():
            path.unlink()
        if created_dir:
            # only our own now-empty folder, never one that was already there
            try:
                uploads.rmdir()
            except OSError:
                pass


# Every spelling that must not reach the uploads folder. The plain path is
# caught by the belt-and-braces route; the rest slip past it and are the real
# test of the mount's own guard.
BYPASSES = [
    "/static/uploads/w4.pdf",  # plain
    "/static//uploads/w4.pdf",  # double slash
    "/static/css/%2e%2e/uploads/w4.pdf",  # %2e%2e traversal out of css/
    "/static/css%2f%2e%2e%2fuploads/w4.pdf",  # %2f-encoded slashes
    "/static/./uploads/w4.pdf",  # ./uploads
    "/static/uploads\\w4.pdf",  # backslash separator (Windows)
    "/static/css%5c%2e%2e%5cuploads/w4.pdf",  # %5c backslash traversal (Windows)
    "/static/UPLOADS/w4.pdf",  # mixed case (case-insensitive FS)
]


@pytest.mark.parametrize("url", BYPASSES)
def test_the_uploads_folder_is_never_served(unauthed_client, uploads_file, url):
    for method in (unauthed_client.get, unauthed_client.head):
        r = method(url)
        assert r.status_code == 404, f"{url} -> {r.status_code}\n{r.text[:200]}"
        assert SECRET not in r.content


def test_ordinary_static_assets_are_still_served(unauthed_client):
    r = unauthed_client.get("/static/css/style.css")
    assert r.status_code == 200, r.text
    assert r.content  # the real file, not an empty 200
