"""Strict value-only BIFF5/8 XLS reader, pinned to reviewed xlrd 2.0.2."""

import io
import struct
from app.portfolio.service import fail, Problem

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _check_records(stream):
    """xlrd returns formula caches; reject formula/encryption records first."""
    if len(stream) < 8:
        fail("invalid_workbook", "Truncated XLS workbook stream")
    opcode, length, version = struct.unpack_from("<HHH", stream)
    if opcode != 0x0809 or length < 4 or version not in (0x0500, 0x0600):
        fail("xls_version", "Use a BIFF5 or BIFF8 XLS export, or CSV/XLSX values")
    position = 0
    saw_eof = False
    while position < len(stream):
        if position + 4 > len(stream):
            fail("invalid_workbook", "Truncated XLS record header")
        opcode, length = struct.unpack_from("<HH", stream, position)
        if opcode == 0 and length == 0:
            if any(stream[position:]):
                fail("invalid_workbook", "XLS contains a malformed padding record")
            break  # Inspect zero-filled OLE padding only once, not every record.
        position += 4
        if position + length > len(stream):
            fail("invalid_workbook", "Truncated XLS record payload")
        if opcode == 0x002F:
            fail(
                "encrypted_workbook",
                "Encrypted XLS exports are unsupported; export unencrypted values",
            )
        if opcode in (0x0006, 0x0206, 0x0406, 0x0221, 0x04BC):
            fail(
                "formula_cell",
                "XLS contains formulas or formula caches; export values instead",
            )
        saw_eof = saw_eof or opcode == 0x000A
        position += length
    if not saw_eof:
        fail("invalid_workbook", "XLS workbook has no complete substream")


def xls_rows(content, sheet=None):
    import xlrd
    from xlrd.compdoc import CompDoc, CompDocError

    if not content.startswith(OLE_SIGNATURE):
        fail("file_type", "The .xls filename must contain a genuine OLE BIFF workbook")
    try:
        container = CompDoc(
            content, logfile=io.StringIO(), ignore_workbook_corruption=False
        )
        stream = container.get_named_stream("Workbook")
        if stream is None:
            stream = container.get_named_stream("Book")
        if stream is None:
            if container.get_named_stream("EncryptedPackage") is not None:
                fail(
                    "encrypted_workbook", "Encrypted spreadsheet files are unsupported"
                )
            fail("file_type", "The OLE file contains no XLS workbook stream")
        _check_records(stream)
        book = xlrd.open_workbook(
            file_contents=content,
            on_demand=True,
            logfile=io.StringIO(),
            ignore_workbook_corruption=False,
        )
        try:
            if book.biff_version not in (50, 70, 80):
                fail("xls_version", "Only BIFF5/7/8 XLS exports are supported")
            if sheet is None and book.nsheets != 1:
                fail(
                    "sheet_required",
                    "Select an explicit sheet from a multi-sheet export",
                )
            selected = book.sheet_by_name(sheet) if sheet else book.sheet_by_index(0)
            if selected.nrows > 20001 or selected.ncols > 256:
                fail(
                    "row_limit", "Split exports into files of at most 20,000 data rows"
                )
            rows = []
            for number in range(selected.nrows):
                row = []
                for cell in selected.row(number):
                    if cell.ctype in (xlrd.XL_CELL_ERROR, xlrd.XL_CELL_BOOLEAN):
                        fail(
                            "invalid_cell",
                            f"XLS row {number + 1} contains a boolean or error cell",
                        )
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        if book.datemode == 0 and 60 <= cell.value < 61:
                            fail(
                                "invalid_date",
                                "XLS contains the nonexistent Excel date 1900-02-29",
                            )
                        value = xlrd.xldate_as_datetime(cell.value, book.datemode)
                        if value.time().isoformat() != "00:00:00":
                            fail(
                                "invalid_date",
                                f"XLS row {number + 1} contains a time of day",
                            )
                        row.append(value.date())
                    else:
                        row.append(cell.value)
                rows.append(row)
            return selected.name, rows
        finally:
            book.release_resources()
    except Problem:
        raise
    except (
        xlrd.XLRDError,
        CompDocError,
        struct.error,
        IndexError,
        KeyError,
        ValueError,
        TypeError,
    ):
        fail(
            "invalid_workbook",
            "The XLS workbook is corrupt, unsupported, or has no selected sheet",
        )
