"""
Security hardening around the AI provider layer:

- CLOUDFLARE_ACCOUNT_ID_RE must only accept 32 lower-hex chars
- validate_worker_url() must reject every SSRF / scheme-confusion vector
"""

import pytest

from app.services.ai_service import (
    CLOUDFLARE_ACCOUNT_ID_RE,
    validate_worker_url,
)

# -------- Account ID regex --------


def test_account_id_accepts_32_lower_hex():
    assert CLOUDFLARE_ACCOUNT_ID_RE.fullmatch("a" * 32)
    assert CLOUDFLARE_ACCOUNT_ID_RE.fullmatch("0123456789abcdef0123456789abcdef")


def test_account_id_rejects_uppercase():
    assert not CLOUDFLARE_ACCOUNT_ID_RE.fullmatch("A" * 32)


def test_account_id_rejects_short():
    assert not CLOUDFLARE_ACCOUNT_ID_RE.fullmatch("a" * 31)


def test_account_id_rejects_long():
    assert not CLOUDFLARE_ACCOUNT_ID_RE.fullmatch("a" * 33)


def test_account_id_rejects_non_hex():
    assert not CLOUDFLARE_ACCOUNT_ID_RE.fullmatch("g" * 32)


def test_account_id_rejects_empty():
    assert not CLOUDFLARE_ACCOUNT_ID_RE.fullmatch("")


# -------- validate_worker_url() attack vectors --------

SSRF_VECTORS = [
    "",
    "   ",
    "http://example.com/v1/chat/completions",  # plain http
    "http://localhost/v1",
    "https://localhost/v1",
    "https://127.0.0.1/v1",
    "https://10.0.0.5/v1",
    "https://192.168.1.1/v1",
    "https://172.16.0.1/v1",
    "https://169.254.169.254/latest/meta-data/",  # AWS metadata
    "https://[::1]/v1",  # IPv6 loopback
    "https://user:pass@workers.dev/v1",  # embedded creds
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://workers.dev/v1",
    "https://workers.dev" + "/x" * 4096,  # oversize
    "https:// workers.dev/v1",  # whitespace
]


@pytest.mark.parametrize("bad_url", SSRF_VECTORS)
def test_validate_worker_url_rejects_ssrf(bad_url):
    with pytest.raises(Exception):
        validate_worker_url(bad_url)


def test_validate_worker_url_accepts_valid_workers_dev():
    ok = validate_worker_url(
        "https://slowbooks-ai.example.workers.dev/v1/chat/completions"
    )
    assert ok.startswith("https://")
    assert "workers.dev" in ok


# -------- custom provider: same URL validation as cloudflare_worker --------
# The `custom` provider reuses validate_worker_url() for its endpoint_url, so
# every SSRF/scheme vector above applies equally. These tests pin that the
# custom provider's endpoint (e.g. an OpenAI-compatible cloud API) is subject
# to the identical allowlist — a public HTTPS endpoint passes, private/LAN
# and non-HTTPS fail.


def test_custom_endpoint_accepts_public_https():
    ok = validate_worker_url("https://api.commandcode.ai/provider/v1")
    assert ok == "https://api.commandcode.ai/provider/v1"
    ok2 = validate_worker_url("https://api.example.com/v1")
    assert ok2 == "https://api.example.com/v1"


def test_custom_endpoint_rejects_lan_and_private():
    for bad in (
        "http://api.example.com/v1",  # plain http (MITM)
        "https://192.168.1.50/v1",  # private
        "https://10.0.0.8/v1",  # private
        "https://127.0.0.1:11434/v1",  # loopback (local ollama etc.)
        "https://localhost/v1",  # localhost
        "https://user:pass@api.example.com/v1",  # embedded creds
        "https://api.example.com/" + "x" * 4096,  # oversize
    ):
        with pytest.raises(ValueError):
            validate_worker_url(bad)
