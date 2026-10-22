"""Deterministic CSV/XLSX normalization; no guessed dates or account identities."""

import csv
import hashlib
import io
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from app.portfolio.service import fail


def parse_bank(content, filename, mapping, date_format="%Y-%m-%d", sheet=None):
    if len(content) > 25 * 1024 * 1024:
        fail("file_size", "Bank import limit is 25 MB per file")
    source_sheet = None
    physical_rows = []
    if filename.lower().endswith(".xlsx"):
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
        try:
            ws = book[sheet] if sheet else book.active
            iterator = ws.iter_rows(values_only=True)
            headers = next(iterator, None)
            if not headers:
                fail("empty_file", "Spreadsheet has no header row")
            rows = [dict(zip([str(h or "") for h in headers], row)) for row in iterator]
            physical_rows = list(range(2, len(rows) + 2))
            source_sheet = ws.title
        finally:
            book.close()
    elif filename.lower().endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1252")
        reader = csv.reader(io.StringIO(text))
        headers = next(reader, [])
        rows = []
        while True:
            physical_start = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if not row:
                continue
            rows.append(dict(zip(headers, row)))
            physical_rows.append(physical_start)
    else:
        fail(
            "file_type",
            "Import CSV or XLSX exports; link PDF statements as supporting documents",
        )
    if len(rows) > 20000:
        fail("row_limit", "Split exports into files of at most 20,000 rows")

    def money(value):
        text = str(value or "0").strip().replace("$", "").replace(",", "")
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        try:
            number = Decimal(text)
        except InvalidOperation:
            fail("invalid_amount", f"Invalid bank amount: {text[:50]}")
        if not number.is_finite() or number * 100 != (number * 100).to_integral_value():
            fail("invalid_amount", "Bank amounts must be finite and exact to cents")
        return int(number * 100)

    result = []
    for number, row in zip(physical_rows, rows, strict=True):
        if all(v is None or str(v).strip() == "" for v in row.values()):
            continue

        def cell(key, default=""):
            column = mapping.get(key)
            if column and column not in row:
                fail("missing_column", f"Column {column} is missing")
            value = row.get(column, default)
            if isinstance(value, str) and value.startswith("="):
                fail(
                    "formula_cell",
                    f"Row {number} contains an unevaluated formula; export values instead",
                )
            return value

        raw = cell("date")
        try:
            day = (
                raw.date()
                if isinstance(raw, datetime)
                else (
                    raw
                    if isinstance(raw, date)
                    else datetime.strptime(str(raw).strip(), date_format).date()
                )
            )
        except ValueError:
            fail("invalid_date", f"Row {number}: date does not match {date_format}")
        amount = (
            money(cell("amount"))
            if mapping.get("amount")
            else money(cell("credit")) - money(cell("debit"))
        )
        result.append(
            {
                "physical_row": number,
                "source_sheet": source_sheet,
                "date": day.isoformat(),
                "amount_cents": amount,
                "description": str(cell("description") or ""),
                "reference": str(cell("reference") or ""),
                "external_id": (
                    str(cell("external_id"))
                    if cell("external_id") not in ("", None)
                    else None
                ),
            }
        )
    if not result:
        fail("empty_file", "No bank transactions found")
    return {
        "source_name": filename,
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "rows": result,
    }
