# ============================================================================
# Receipt / Document Intake — Tier 2 OCR tests.
#
# Unit tests exercise the deterministic parsers directly; endpoint tests mock
# the Tesseract call (ocr_image) so they run everywhere, including CI without
# the binary. Integration tests against real Tesseract + the ground-truth
# PDFs in assets/sample-receipts land in the finishing-touches slice.
# ============================================================================

import os
from datetime import datetime, timedelta, timezone

from app.services import ocr_service

# Mirror of assets/sample-receipts/vendor-office-supply-receipt.pdf
CANNED_RECEIPT_TEXT = """ACME OFFICE SUPPLY CO.
4820 Industrial Parkway
Columbus, OH 43215
(614) 555-0164
Invoice/Receipt #: 88312    Date: 08/14/2026
Staples, 1/2" (box)      $8.75
Toner Cartridge, black  $54.99
Printer Paper, 500 ct   $7.99
Subtotal    $71.73
Tax (7.25%) $5.20
TOTAL      $76.93
VISA **** 2271
"""


def _png_bytes() -> bytes:
    """Arbitrary image-ish bytes. The route no longer decodes images with PIL
    (Tesseract reads them natively), and ocr_image_bytes is mocked in these
    endpoint tests, so content is irrelevant here."""
    return b"\x89PNG\r\n\x1a\nfake-image-data"


# ---------------------------------------------------------------------------
# Parser units
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_mmddyyyy(self):
        assert ocr_service.parse_date("Date: 08/14/2026") == "2026-08-14"

    def test_iso(self):
        assert ocr_service.parse_date("2026-08-14") == "2026-08-14"

    def test_month_name(self):
        assert ocr_service.parse_date("Aug 14, 2026") == "2026-08-14"

    def test_day_month_name(self):
        assert ocr_service.parse_date("14 Aug 2026") == "2026-08-14"

    def test_invalid_date_ignored(self):
        assert ocr_service.parse_date("13/45/2026") is None

    def test_first_date_wins(self):
        text = "Card valid thru 01/2027\nDate: 08/14/2026"
        assert ocr_service.parse_date(text) == "2026-08-14"

    def test_missing(self):
        assert ocr_service.parse_date("no dates here") is None


class TestParseTotal:
    def test_anchor_high_confidence(self):
        total, conf = ocr_service.parse_total(CANNED_RECEIPT_TEXT)
        assert total == "76.93" and conf == "high"

    def test_amount_due_anchor(self):
        total, conf = ocr_service.parse_total("AMOUNT DUE\n$123.45")
        assert total == "123.45" and conf == "high"

    def test_fallback_largest_low_confidence(self):
        total, conf = ocr_service.parse_total("Item $5.00\nItem $12.50\nItem $8.00")
        assert total == "12.50" and conf == "low"

    def test_tip_not_picked_as_total(self):
        text = "Subtotal $48.00\nTax $4.20\nTip $9.00\nTOTAL $52.20"
        total, conf = ocr_service.parse_total(text)
        assert total == "52.20" and conf == "high"

    def test_thousands_separator(self):
        total, conf = ocr_service.parse_total("TOTAL $1,234.56")
        assert total == "1234.56" and conf == "high"

    def test_missing(self):
        total, conf = ocr_service.parse_total("no amounts here")
        assert total is None and conf == "missing"


class TestParseTax:
    def test_tax_and_subtotal(self):
        tax, subtotal = ocr_service.parse_tax(CANNED_RECEIPT_TEXT)
        assert tax == "5.20" and subtotal == "71.73"

    def test_tax_included_excluded(self):
        tax, subtotal = ocr_service.parse_tax(
            "Subtotal $10.00\nTax included\nTotal $10.00"
        )
        assert tax is None

    def test_no_tax_line(self):
        tax, subtotal = ocr_service.parse_tax("Unleaded $39.31\nTOTAL $39.31")
        assert tax is None


class TestParseMerchant:
    def test_first_line_high_confidence(self):
        val, conf = ocr_service.parse_merchant(CANNED_RECEIPT_TEXT)
        assert val == "ACME OFFICE SUPPLY CO." and conf == "high"

    def test_skips_amount_and_date_lines(self):
        val, conf = ocr_service.parse_merchant("$76.93\n08/14/2026\nACME SUPPLY")
        assert val == "ACME SUPPLY" and conf == "low"

    def test_skips_phone_number(self):
        val, conf = ocr_service.parse_merchant("(614) 555-0164\nACME SUPPLY")
        assert val == "ACME SUPPLY"

    def test_missing(self):
        val, conf = ocr_service.parse_merchant("$1.00\n08/14/2026\n")
        assert val is None and conf == "missing"


class TestExtract:
    def test_full_receipt(self):
        r = ocr_service.extract_receipt(CANNED_RECEIPT_TEXT)
        assert r["merchant"]["value"] == "ACME OFFICE SUPPLY CO."
        assert r["merchant"]["confidence"] == "high"
        assert r["date"] == "2026-08-14"
        assert r["total"] == "76.93"
        assert r["total_confidence"] == "high"
        assert r["subtotal"] == "71.73"
        assert r["tax"] == "5.20"
        assert r["tax_detected"] is True
        assert r["partial_reasons"] == []

    def test_blank_receipt_flags_partial(self):
        # single-word OCR junk — no merchant, no amounts, no dates
        r = ocr_service.extract_receipt("ZZZZZZ\nQQQQQQ\n")
        assert r["total"] is None
        assert r["merchant"]["value"] is None
        assert any("total not detected" in x for x in r["partial_reasons"])


# ---------------------------------------------------------------------------
# Endpoint tests (Tesseract mocked). A pending scan is kept in the company's
# own database (stored_files, kind receipt_scan) since 2.18.0: the intake
# folder was shared by every company on a desktop install.
# ---------------------------------------------------------------------------


def _intake_row(db_session, intake_id):
    """The pending scan's stored file, or None."""
    from app.models.stored_files import StoredFile

    db_session.expire_all()
    return (
        db_session.query(StoredFile)
        .filter(StoredFile.token == intake_id, StoredFile.kind == "receipt_scan")
        .first()
    )


def _scan(client, monkeypatch, text=CANNED_RECEIPT_TEXT) -> str:
    """Helper: run a mocked scan and return the intake id."""
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr_service, "ocr_language", lambda: "eng")
    monkeypatch.setattr(
        ocr_service, "ocr_image_words", lambda data, lang=None: (text, [])
    )
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 200, r.text
    return r.json()["intake_id"]


def test_status_available(client, monkeypatch):
    monkeypatch.setattr(
        ocr_service,
        "tesseract_info",
        lambda: {"available": True, "version": "5.5.0", "languages": ["eng"]},
    )
    r = client.get("/api/ocr/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["version"] == "5.5.0"
    assert body["languages"] == ["eng"]


def test_requires_auth(unauthed_client):
    assert unauthed_client.get("/api/ocr/status").status_code == 401
    assert (
        unauthed_client.post(
            "/api/ocr/receipt",
            files={"file": ("r.png", _png_bytes(), "image/png")},
        ).status_code
        == 401
    )


def test_scan_happy_path(client, db_session, monkeypatch):
    intake_id = _scan(client, monkeypatch)
    # the scan is kept, bytes and all, in this company's database
    row = _intake_row(db_session, intake_id)
    assert row is not None and row.original_name == "receipt.png"
    assert row.data == _png_bytes() and row.content_type == "image/png"


def test_scan_response_fields(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr_service, "ocr_language", lambda: "eng")
    monkeypatch.setattr(
        ocr_service,
        "ocr_image_words",
        lambda data, lang=None: (CANNED_RECEIPT_TEXT, []),
    )
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ocr_available"] is True
    assert len(body["intake_id"]) == 32  # uuid4().hex
    assert body["merchant"]["value"] == "ACME OFFICE SUPPLY CO."
    assert body["date"] == "2026-08-14" and body["date_is_default"] is False
    assert body["total"] == "76.93" and body["total_confidence"] == "high"
    assert body["subtotal"] == "71.73"
    assert body["tax"] == "5.20" and body["tax_detected"] is True
    assert body["language"] == "eng"
    assert body["multi_page"] is False
    assert body["partial"] is False and body["partial_reasons"] == []
    assert "ACME OFFICE SUPPLY" in body["raw_text"]


def test_scan_tesseract_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: False)
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ocr_available"] is False
    assert "Tesseract OCR is not installed" in body["message"]
    assert body["intake_id"] is None


def test_scan_missing_date_defaults_today(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr_service, "ocr_language", lambda: "eng")
    monkeypatch.setattr(
        ocr_service,
        "ocr_image_words",
        lambda data, lang=None: ("MY VENDOR\nTOTAL $10.00\n", []),
    )
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["date"] is not None and body["date_is_default"] is True
    assert body["partial"] is True
    assert any("today's date" in x for x in body["partial_reasons"])


def test_scan_unusable_language_data(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr_service, "ocr_language", lambda: None)
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["ocr_available"] is False
    assert "language data" in r.json()["message"]


def test_scan_bad_content_type(client, monkeypatch, tmp_path):
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


def test_scan_oversize(client, monkeypatch, tmp_path):
    from fastapi import HTTPException

    async def tiny_read_limited(file, max_bytes=0, label="File"):
        content = await file.read(1024 + 1)
        if len(content) > 1024:
            raise HTTPException(status_code=413, detail=f"{label} too large")
        return content

    monkeypatch.setattr("app.routes.ocr.read_limited", tiny_read_limited)
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("big.png", b"x" * 2048, "image/png")},
    )
    assert r.status_code == 413


def test_scan_pdf_multi_page(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr_service, "ocr_language", lambda: "eng")
    monkeypatch.setattr(
        ocr_service,
        "ocr_image_words",
        lambda data, lang=None: (CANNED_RECEIPT_TEXT, []),
    )
    monkeypatch.setattr(
        ocr_service,
        "rasterize_pdf",
        lambda data, dpi=200: (b"\x89PNG-rasterized", 3),
    )
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 200
    assert r.json()["multi_page"] is True


def test_scan_pdf_without_any_renderer_400(client, monkeypatch, tmp_path):
    """No native renderer and no poppler: a 400 that names the fix for this
    platform (Linux wording here; see test_pdf_raster for the others)."""
    from app.services import pdf_raster

    from app.services import ocr_engines

    monkeypatch.setattr(ocr_service, "tesseract_available", lambda: True)
    monkeypatch.setattr(ocr_service, "poppler_available", lambda: False)
    monkeypatch.setattr(pdf_raster, "windows_available", lambda: False)
    monkeypatch.setattr(pdf_raster, "macos_available", lambda: False)
    monkeypatch.setattr(pdf_raster.sys, "platform", "linux")

    # The route answers 200 with ocr_available=False when the ENGINE is
    # unavailable, before it ever reaches the rasterizer — which is what a box
    # without tesseract does, and why this test failed on Windows (#121).
    # Give it a working engine so the PDF path is the thing under test.
    class _Engine:
        def unavailable_reason(self):
            return None

        def recognize(self, data):
            return "irrelevant — the rasterizer must refuse first"

    monkeypatch.setattr(ocr_engines, "get_engine", lambda *_a, **_k: _Engine())
    r = client.post(
        "/api/ocr/receipt",
        files={"file": ("receipt.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 400
    assert "poppler-utils" in r.json()["detail"]
    assert "images still work" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Intake lifecycle
# ---------------------------------------------------------------------------


def test_attach_to_invoice(
    client, db_session, monkeypatch, tmp_path, seed_accounts, seed_customer
):
    intake_id = _scan(client, monkeypatch)

    inv = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-08-01",
            "terms": "Net 30",
            "lines": [{"description": "Test line", "quantity": 1, "rate": "10.00"}],
        },
    )
    assert inv.status_code == 201, inv.text
    inv_id = inv.json()["id"]

    r = client.post(
        f"/api/ocr/intake/{intake_id}/attach",
        json={"entity_type": "invoice", "entity_id": inv_id},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["entity_type"] == "invoice"
    assert body["entity_id"] == inv_id
    assert "receipt.png" in body["filename"]

    from app.models.attachments import Attachment

    row = (
        db_session.query(Attachment)
        .filter(Attachment.entity_type == "invoice", Attachment.entity_id == inv_id)
        .first()
    )
    assert row is not None
    # intake consumed: its stored file is now the attachment's, not a copy
    assert _intake_row(db_session, intake_id) is None
    assert row.stored_file.kind == "attachment" and row.stored_file.token is None
    download = client.get(f"/api/attachments/download/{row.id}")
    assert download.status_code == 200 and download.content == _png_bytes()


def test_attach_to_expense(client, db_session, monkeypatch, tmp_path, seed_accounts):
    """Paid receipts land on the one-step Expense form; the scan attaches to
    the posted transaction."""
    intake_id = _scan(client, monkeypatch)
    exp = client.post(
        "/api/expenses",
        json={
            "date": "2026-09-02",
            "payee": "Sweet Forest Cafe",
            "expense_account_id": seed_accounts["6000"].id,
            "paid_from_account_id": seed_accounts["1000"].id,
            "amount": "30.30",
        },
    )
    assert exp.status_code == 201, exp.text
    exp_id = exp.json()["id"]

    r = client.post(
        f"/api/ocr/intake/{intake_id}/attach",
        json={"entity_type": "expense", "entity_id": exp_id},
    )
    assert r.status_code == 201, r.text
    assert r.json()["entity_type"] == "expense"
    listed = client.get(f"/api/attachments/expense/{exp_id}")
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert _intake_row(db_session, intake_id) is None

    # A non-expense transaction id is not an expense.
    intake_id = _scan(client, monkeypatch)
    r = client.post(
        f"/api/ocr/intake/{intake_id}/attach",
        json={"entity_type": "expense", "entity_id": 999999},
    )
    assert r.status_code == 404


def test_attach_missing_entity(client, db_session, monkeypatch):
    intake_id = _scan(client, monkeypatch)
    r = client.post(
        f"/api/ocr/intake/{intake_id}/attach",
        json={"entity_type": "invoice", "entity_id": 999999},
    )
    assert r.status_code == 404
    # intake survives a failed attach
    assert _intake_row(db_session, intake_id) is not None


def test_attach_bad_entity_type(client, monkeypatch, tmp_path):
    intake_id = _scan(client, monkeypatch)
    r = client.post(
        f"/api/ocr/intake/{intake_id}/attach",
        json={"entity_type": "vendor", "entity_id": 1},
    )
    assert r.status_code == 400


def _backdate(db_session, intake_id, hours):
    row = _intake_row(db_session, intake_id)
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    db_session.commit()


def test_attach_expired_intake_404(client, db_session, monkeypatch):
    intake_id = _scan(client, monkeypatch)
    # backdate the scan beyond the TTL
    _backdate(db_session, intake_id, 25)
    r = client.post(
        f"/api/ocr/intake/{intake_id}/attach",
        json={"entity_type": "invoice", "entity_id": 1},
    )
    assert r.status_code == 404
    assert "expired" in r.json()["detail"]


def test_delete_intake(client, db_session, monkeypatch):
    intake_id = _scan(client, monkeypatch)
    assert client.delete(f"/api/ocr/intake/{intake_id}").status_code == 200
    assert _intake_row(db_session, intake_id) is None
    # idempotent
    assert client.delete(f"/api/ocr/intake/{intake_id}").status_code == 200


def test_sweep_expires_old_intakes(db_session):
    old_id = ocr_service.save_intake(db_session, b"x", "old.png", "image/png")
    fresh_id = ocr_service.save_intake(db_session, b"y", "fresh.png", "image/png")
    # backdate only the OLD intake (after both saves, since each save sweeps)
    _backdate(db_session, old_id, 25)

    assert ocr_service.sweep_intake(db_session) == 1
    db_session.commit()
    assert ocr_service.get_intake(db_session, old_id) is None
    assert ocr_service.get_intake(db_session, fresh_id)["data"] == b"y"


def test_get_intake_rejects_traversal(db_session):
    assert ocr_service.get_intake(db_session, "..%2f..%2fetc") is None
    assert ocr_service.get_intake(db_session, "nothex") is None


# ---------------------------------------------------------------------------
# Direct subprocess plumbing (no pytesseract)
# ---------------------------------------------------------------------------


def _fake_tesseract(bin_dir, body_sh: str, body_bat: str, mkdir: bool = False):
    """Write a fake `tesseract` the OS can actually execute.

    The suite used a `#!/bin/sh` script, which Windows cannot run — two OCR
    tests failed there for that reason alone (issue #121). On Windows the
    shim is a .bat, which is what `shutil.which` finds and subprocess runs.
    """
    import sys as _sys

    if mkdir:
        bin_dir.mkdir(parents=True, exist_ok=True)
    if _sys.platform == "win32":
        fake = bin_dir / "tesseract.bat"
        fake.write_text(body_bat, encoding="utf-8")
    else:
        fake = bin_dir / "tesseract"
        fake.write_text(body_sh, encoding="utf-8")
        fake.chmod(0o755)
    return fake


def test_direct_subprocess_tesseract(tmp_path, monkeypatch):
    """Prove the no-wrapper design: a fake `tesseract` on PATH is invoked via
    subprocess with stdin/stdout, and its stdout becomes the OCR text. This
    exercises the real plumbing (args, stdin pipe, stdout parse) without the
    real binary — so it runs in CI even when tesseract is absent."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_tesseract(
        bin_dir,
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        '  echo "tesseract 9.9.9"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "--list-langs" ]; then\n'
        '  echo "eng"\n'
        "  exit 0\n"
        "fi\n"
        "cat > /dev/null\n"
        'printf "FAKE MERCHANT\\nTOTAL $42.00\\n"\n',
        "@echo off\r\n"
        'if "%~1"=="--version" (echo tesseract 9.9.9 & exit /b 0)\r\n'
        'if "%~1"=="--list-langs" (echo eng & exit /b 0)\r\n'
        "echo FAKE MERCHANT\r\n"
        "echo TOTAL $42.00\r\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # tesseract_info is cached for 60s — force a fresh probe against the fake
    monkeypatch.setattr(ocr_service, "_cache", {"at": 0.0, "info": None})

    assert ocr_service.tesseract_available() is True
    info = ocr_service.tesseract_info()
    assert info["version"] == "9.9.9"
    assert "eng" in info["languages"]
    assert ocr_service.ocr_language() == "eng"

    text = ocr_service.ocr_image_bytes(b"pretend-image-bytes", lang="eng")
    assert "FAKE MERCHANT" in text
    assert "TOTAL" in text


def test_ocr_image_bytes_failure_raises(tmp_path, monkeypatch):
    """A nonzero tesseract exit surfaces as OCRRuntimeError, not a crash."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_tesseract(
        bin_dir,
        "#!/bin/sh\necho 'bad image data' >&2\nexit 2\n",
        "@echo off\r\necho bad image data 1>&2\r\nexit /b 2\r\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ocr_service, "_cache", {"at": 0.0, "info": None})

    import pytest

    with pytest.raises(ocr_service.OCRRuntimeError) as excinfo:
        ocr_service.ocr_image_bytes(b"junk", lang="eng")
    assert "exit 2" in str(excinfo.value)


def test_parse_tax_looks_ahead_across_split_lines():
    """WinRT puts anchor and amount on separate lines; subtotal/tax get the
    same bounded lookahead as total (VH308 hardware-lap regression)."""
    text = "NEON PULSE\nSUBTOTAL\n46.24\nTAX 6.25%\n2.89\nTOTAL\n49.13\n"
    tax, subtotal = ocr_service.parse_tax(text)
    assert tax == "2.89"
    assert subtotal == "46.24"


def test_parse_tax_trailing_anchor_does_not_crash():
    tax, subtotal = ocr_service.parse_tax("stuff\nSUBTOTAL\nTAX")
    assert tax is None and subtotal is None


# ---------------------------------------------------------------------------
# Real-receipt parser behaviour (SROIE corpus eval, 2026-09-02).  The
# synthetic receipts hid every one of these; real thermal prints exposed
# them.  Texts below are trimmed OCR output with the merchant renamed.
# ---------------------------------------------------------------------------

GST_RECEIPT = """\
SAMPLE HARDWARE TRADING
NO 290, JALAN AIR PANAS.
| SIMPLIFIED TAX INVOICE
Salesperson ~ Ref. :
4 742 7.42 SR
"Tetal Oy" 4 742
| Total Sales (Excluding GST) . 7.00
if Discount ‘ 0.00
: TotalGST . 0.42
| Rounding . 0.00
| Total Sales (inclusive of GST) : ( 7.42)
{ CASH : T.A2
Change : 0.00
GST SUMMARY
Tax Code % Amt (RM) Tax (RM)
SR 6 7 00 042
Totel: 7.00 0.42
"""


def test_parse_total_prefers_tax_inclusive_total_over_first_anchor():
    """GST/VAT receipts print "Total (Excluding GST)" BEFORE the real total;
    first-anchor-wins picked the pre-tax figure on 11/20 real receipts."""
    total, conf = ocr_service.parse_total(GST_RECEIPT)
    assert (total, conf) == ("7.42", "high")


def test_parse_tax_reads_gst_and_excluding_total_as_subtotal():
    tax, subtotal = ocr_service.parse_tax(GST_RECEIPT)
    assert tax == "0.42"
    assert subtotal == "7.00"


def test_parse_total_handles_ocr_misspellings_of_total():
    text = "Cashier: X\nTatal (Excluding GST): 4130\nGST Payable: 2.f3\ntotal (Inclusive af GST): 48.15\nTOTAL: 48.15\nCASH : 50.60\n"
    assert ocr_service.parse_total(text) == ("48.15", "high")
    # "Tota!" — the fuzzy word must still be recognised as a subtotal anchor
    _, subtotal = ocr_service.parse_tax(
        "Tota! ':ales (Excluding GST) - 89 50\nTotal GST : 5.37\n"
    )
    assert subtotal == "89.50"


def test_parse_total_takes_post_rounding_total_when_no_strong_anchor():
    text = "Subtotal 87.80\nGST @6% 5.81\nService 8.78\nTotal: 102.39\nTotal 102.40\nCASH 110.00\n"
    assert ocr_service.parse_total(text) == ("102.40", "high")


def test_parse_total_lookahead_only_accepts_amount_only_line():
    """A column header ending in TOTAL / TAX must not swallow the first line
    item on the next line (h007: total=22.50, tax=22.50 before the fix)."""
    text = (
        "Description Qty U.price Total TAX\nBURGER 1 x 22.50 22.50 SR\nTOTAL: 48.15\n"
    )
    assert ocr_service.parse_total(text) == ("48.15", "high")
    tax, _ = ocr_service.parse_tax(text)
    assert tax is None
    # ...but a genuinely split anchor/amount pair still works (WinRT).
    assert ocr_service.parse_total("TOTAL\n49.13\n") == ("49.13", "high")


def test_parse_total_loose_decimal_on_anchor_line():
    """OCR drops the decimal point on thermal prints: "7 00", "140. 00"."""
    assert ocr_service.parse_total(
        "Subtotal 7.06\nTotal Incl. of GST 7 00\nPayment 7.00\n"
    ) == ("7.00", "high")
    assert ocr_service.parse_total("Total Sales (Inclusive of GST) : 140. 00\n") == (
        "140.00",
        "high",
    )


def test_parse_total_skips_excluded_and_zero_anchors():
    text = "Total Qty 3 23.32\nTotal Items: 3\nSub Total 22.00\nTotal GST 1.32\nBalance Due 0.00\nTOTAL 23.32\n"
    assert ocr_service.parse_total(text) == ("23.32", "high")
    tax, subtotal = ocr_service.parse_tax(text)
    assert (tax, subtotal) == ("1.32", "22.00")


def test_parse_tax_ignores_total_lines_that_mention_gst():
    """ "Tota] RM Sucluding GST 6% 73.30" is a total line, not the tax."""
    text = "Tota] RM Sucluding GST 6% 73.30\nRounding 0.00\nTotal Rounded 73.30\nGST SR 69.15 4.18\n"
    tax, _ = ocr_service.parse_tax(text)
    assert tax == "4.18"
    assert ocr_service.parse_total(text) == ("73.30", "high")


def test_parse_tax_skips_headers_and_percent_only_lines():
    text = "Tax Invoice\nGST ID : 000750673920\nGST 6.00 % RO. 2\nTax Code % Amt (RM) Tax (RM)\nSR 6 12.50 0.75\n"
    tax, _ = ocr_service.parse_tax(text)
    assert tax is None


def test_extract_receipt_reconciles_pre_tax_total_with_tax():
    """Total == subtotal means we read the pre-tax line; with tax known the
    real total is the sum, offered at low confidence."""
    r = ocr_service.extract_receipt(
        "SHOP NAME HERE\nSub Total 28.00\nTax 1.68\nTotal 28.00\n"
    )
    assert r["total"] == "29.68"
    assert r["total_confidence"] == "low"
    assert any("low-confidence" in x for x in r["partial_reasons"])


def test_parse_merchant_skips_ocr_noise_lines():
    text = "0) y BO3Z0ly\n—  ByBOlOtd\nSWEET FOREST CAFE\nNO 21,JLN BUNGA KANTAN\n"
    merchant, conf = ocr_service.parse_merchant(text)
    assert merchant == "SWEET FOREST CAFE"
    assert conf == "low"


def test_tesseract_probe_skips_inaccessible_optional_installation(
    tmp_path, monkeypatch
):
    class Inaccessible:
        def is_file(self):
            raise PermissionError("Installation directory is not accessible")

    binary = tmp_path / "tesseract"
    binary.write_text("synthetic executable")
    monkeypatch.setattr(ocr_service.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        ocr_service, "_tesseract_candidates", lambda: [Inaccessible(), binary]
    )
    assert ocr_service.tesseract_cmd() == str(binary)
    monkeypatch.setattr(ocr_service, "_tesseract_candidates", lambda: [Inaccessible()])
    assert ocr_service.tesseract_cmd() is None


def test_tesseract_cmd_falls_back_to_stock_install_dir(tmp_path, monkeypatch):
    """The UB Mannheim Windows installer (and Homebrew on a macOS GUI app)
    leave tesseract off PATH; the resolver checks the stock locations."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(ocr_service, "_cache", {"at": 0.0, "info": None})
    # Isolate the missing-install case from Homebrew or any other host
    # install. Emptying PATH is not enough: the resolver's whole job is to
    # look in the stock locations too, and /opt/homebrew/bin/tesseract is
    # one of them — so on a Mac with Homebrew this asserted None against a
    # resolver that was working correctly (@ContractorKeith, v2.10.2 Mac
    # review, and flagged by him as pre-existing back on 2.9.x).
    monkeypatch.setattr(ocr_service, "_tesseract_candidates", lambda: [])
    assert ocr_service.tesseract_cmd() is None
    assert ocr_service.tesseract_info()["available"] is False

    # The fake-install half supplies and verifies its own candidate. The stub
    # must be executable on the host: a #!/bin/sh script is not, on Windows.
    stock = _fake_tesseract(
        tmp_path / "Tesseract-OCR",
        "#!/bin/sh\nexit 0\n",
        "@echo off\r\nexit /b 0\r\n",
        mkdir=True,
    )
    monkeypatch.setattr(ocr_service, "_tesseract_candidates", lambda: [stock])
    monkeypatch.setattr(ocr_service, "_cache", {"at": 0.0, "info": None})
    assert ocr_service.tesseract_cmd() == str(stock)
    info = ocr_service.tesseract_info()
    assert info["available"] is True
    assert info["path"] == str(stock)


def test_tesseract_candidates_windows_locations(monkeypatch):
    monkeypatch.setenv("ProgramFiles", "C:/Program Files")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/x/AppData/Local")
    cands = [c.as_posix() for c in ocr_service._tesseract_candidates(windows=True)]
    assert "C:/Program Files/Tesseract-OCR/tesseract.exe" in cands
    assert "C:/Users/x/AppData/Local/Programs/Tesseract-OCR/tesseract.exe" in cands
    posix = [c.as_posix() for c in ocr_service._tesseract_candidates(windows=False)]
    assert "/opt/homebrew/bin/tesseract" in posix


# ---------------------------------------------------------------------------
# Vendor document number → Bill # / Reference (2026-09-02)
# ---------------------------------------------------------------------------


def test_parse_reference_reads_labeled_document_numbers():
    from app.services.ocr_service import parse_reference

    assert (
        parse_reference("GIN KEE\n(81109-A)\nInvoice No: 7011\nDate: 02/12/2017")
        == "7011"
    )
    assert parse_reference("SHOP\nGST Reg No: 001234567\nReceipt # A-1187") == "A-1187"
    assert parse_reference("Tel: 03-2164 1400\nCheck: 2098213\nTable 41") == "2098213"
    assert parse_reference("Trans No: 00123456\nCard ****1234") == "00123456"


def test_parse_reference_survives_a_lookalike_later_on_the_line():
    from app.services.ocr_service import parse_reference

    # SkyTech lap, build 46: "Pax(s)" after the invoice number was enough
    # to skip the whole line and leave Bill # empty.
    assert parse_reference("INV No.: 593101 Pax(s): 2\nDate : 14-02-2018") == "593101"
    assert parse_reference("Receipt # A-1187 Table 4") == "A-1187"


def test_parse_reference_refuses_unlabeled_and_lookalike_numbers():
    from app.services.ocr_service import parse_reference

    assert parse_reference("SHOP\n(81109-A)\nTOTAL 5.00") is None  # bare reg no
    assert parse_reference("Receipt 02/12/2017\nTOTAL 1.00") is None  # a date
    assert parse_reference("Invoice No.\nTOTAL 1.00") is None  # label alone
    assert parse_reference("GST Reg No: 001234567") is None  # tax registration
    assert parse_reference("Order Tel: 5551234") is None


def test_parse_date_day_first_and_dotted_forms():
    from app.services.ocr_service import parse_date

    # Day-first is the fallback when the US reading is impossible.
    assert parse_date("Date : 14-02-2018 13:02:42") == "2018-02-14"
    assert parse_date("14/02/2018") == "2018-02-14"
    assert parse_date("14.02.18") == "2018-02-14"
    assert parse_date("02-14-2018") == "2018-02-14"
    # Both readings valid → US ordering still wins.
    assert parse_date("12/02/17") == "2017-12-02"
    assert parse_date("2018-02-14") == "2018-02-14"
    # Month names with a two-digit year or dashes/slashes (SROIE lap picks).
    assert parse_date("Chk 8660 Guest0\n28 Mar 18 18:32:36") == "2018-03-28"
    assert parse_date("Date: 05-JAN-2017") == "2017-01-05"
    assert parse_date("02/JAN/2017") == "2017-01-02"
    # Not dates: phone numbers, times, bare digit runs, mixed separators.
    assert parse_date("Tel: 03-2164 1400") is None
    assert parse_date("1-630-423-2040") is None
    assert parse_date("13:02:42") is None
    assert parse_date("14-02/2018") is None
    assert parse_date("2018-13-01") is None


def test_extract_receipt_carries_reference():
    from app.services.ocr_service import extract_receipt

    text = "NEON PULSE TECHSHOP\nInvoice #: NP-2201\nTOTAL 49.13"
    assert extract_receipt(text)["reference"] == "NP-2201"
    assert extract_receipt("SHOP\nTOTAL 1.00")["reference"] is None


# ---------------------------------------------------------------------------
# US register tape (Walmart / Whole Foods, real phone photos, 2026-09-02)
# ---------------------------------------------------------------------------

WALMART_TEXT = (
    "Walmart\nSave money. Live better.\n"
    "ST# 01595 OP# 007573 TE# 07 TR# 08890\n"
    "SUBTOTAL 25.35\nTAX 1 7.000 % 1.25\nTOTAL 26.60\nDEBIT TEND 26.60\n"
    "REF # 712400283994\nNETWORK ID. 0069 APPR CODE 868483\n"
    "TERMINAL # SCO10087\nTC# 3041 7466 0669 7952 272\n05/04/17 14:26:03\n"
)


def test_parse_tax_percent_rate_line_is_not_the_amount():
    """ "TAX 1 7.000 %" read as tax 7.00 on the build-47 lap: a third
    decimal or a trailing % is a rate. The amount is the last figure on
    the line, or the next amount-only line when WinRT splits them."""
    assert ocr_service.parse_tax(WALMART_TEXT) == ("1.25", "25.35")
    split = WALMART_TEXT.replace("TAX 1 7.000 % 1.25", "TAX 1 7.000 %\n1.25")
    assert ocr_service.parse_tax(split) == ("1.25", "25.35")
    assert ocr_service.parse_tax("SUBTOTAL 25.35\nTAX 1 7.000 %\nTOTAL 26.60\n") == (
        None,
        "25.35",
    )
    assert ocr_service.parse_total(WALMART_TEXT) == ("26.60", "high")


def test_parse_reference_prefers_walmart_tc_over_card_auth_ref():
    assert ocr_service.parse_reference(WALMART_TEXT) == "3041 7466 0669 7952 272"
    # No TC# read: the REF # sitting in the card-terminal block is the
    # authorization reference, not the receipt — leave it blank.
    no_tc = WALMART_TEXT.replace("TC# 3041 7466 0669 7952 272\n", "")
    assert ocr_service.parse_reference(no_tc) is None
    # A plain "Ref #" with no terminal block around it is still a reference.
    assert ocr_service.parse_reference("SHOP\nRef # 4451\nTOTAL 1.00") == "4451"


def test_parse_date_skips_return_policy_and_promo_lines():
    """Whole Foods prints the return window ("made on or after 9/15/2020")
    above the transaction date at the bottom of the tape."""
    text = (
        "WHOLE FOODS MARKET\nSubtotal : $28.28\nTotal $28.28\n"
        "Returns will only be accepted on purchases\n"
        "made on or after 9/15/2020. All returns\n"
        "908 6387 02/10/2021 07:10 PM\n"
    )
    assert ocr_service.parse_date(text) == "2021-02-10"
    assert (
        ocr_service.parse_date("Offer valid thru 12/31/2026\nDate 08/14/2026")
        == "2026-08-14"
    )
    assert ocr_service.parse_date("Return by 02/15/2021") is None
