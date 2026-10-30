"""A document the QuickBooks Online import created voids here like any other
(#192 review, 2.18.0).

The import brings QBO's invoices, payments and sales receipts in as
documents with no posting of their own; the ledger import (Posted Ledger
Activity) posts QBO's lines for each. Voiding one here reversed nothing,
because it had nothing of its own to reverse, so A/R Aging and the ledger
drifted apart (and 70634080 then refused the void). Now the void reverses
the import's posting for it, and a later import leaves it alone.
"""

from datetime import date
from decimal import Decimal

import pytest
from quickbooks.objects.invoice import Invoice as QBOInvoice
from quickbooks.objects.payment import Payment as QBOPayment
from quickbooks.objects.salesreceipt import SalesReceipt as QBOSalesReceipt

from app.models.contacts import Customer
from app.models.invoices import Invoice, InvoiceStatus
from app.models.payments import Payment
from app.models.qbo_mapping import QBOMapping
from app.models.transactions import Transaction
from app.services import qbo_import, qbo_ledger_import, qbo_progress
from app.services.bank_register import gl_balances
from tests.test_qbo_import_review import LedgerClient

DAY = date(2026, 8, 3)
CUSTOMER = {"value": "58", "name": "Acme Diner"}


def _sale_line(amount, description):
    return {
        "Id": "1",
        "Amount": amount,
        "Description": description,
        "DetailType": "SalesItemLineDetail",
        "SalesItemLineDetail": {"Qty": 1, "UnitPrice": amount},
    }


def _invoice(qbo_id, number, amount):
    return QBOInvoice.from_json(
        {
            "Id": qbo_id,
            "DocNumber": number,
            "TxnDate": DAY.isoformat(),
            "DueDate": DAY.isoformat(),
            "TotalAmt": amount,
            "Balance": amount,
            "CustomerRef": CUSTOMER,
            "Line": [_sale_line(amount, "Catering")],
        }
    )


class Books:
    """Invoice 1037 (50.00, paid 20.00 by a QBO payment), invoice 1038
    (40.00) and sales receipt SR-9 (30.00, into Undeposited Funds), through
    the real document import; `ledger()` runs the ledger import over QBO's
    General Ledger for them, and a deposit of the 20.00 when `deposited`."""

    def __init__(self, db, seed_accounts, monkeypatch):
        self.db, self.accounts = db, seed_accounts
        customer = Customer(name="Acme Diner", is_active=True)
        db.add(customer)
        db.flush()
        mapped = [("customer", "58", customer.id)]
        mapped += [
            ("account", qbo_id, seed_accounts[number].id)
            for qbo_id, number in [
                ("84", "1100"),
                ("79", "4000"),
                ("35", "1200"),
                ("36", "1000"),
            ]
        ]
        for kind, qbo_id, local_id in mapped:
            db.add(QBOMapping(entity_type=kind, qbo_id=qbo_id, slowbooks_id=local_id))
        db.flush()
        self.sources = {
            QBOInvoice: [_invoice("130", "1037", 50), _invoice("133", "1038", 40)],
            QBOPayment: [
                QBOPayment.from_json(
                    {
                        "Id": "131",
                        "TxnDate": DAY.isoformat(),
                        "TotalAmt": 20,
                        "CustomerRef": CUSTOMER,
                        "DepositToAccountRef": {"value": "35"},
                        "Line": [
                            {
                                "Amount": 20,
                                "LinkedTxn": [{"TxnId": "130", "TxnType": "Invoice"}],
                            }
                        ],
                    }
                )
            ],
            QBOSalesReceipt: [
                QBOSalesReceipt.from_json(
                    {
                        "Id": "132",
                        "DocNumber": "SR-9",
                        "TxnDate": DAY.isoformat(),
                        "TotalAmt": 30,
                        "CustomerRef": CUSTOMER,
                        "DepositToAccountRef": {"value": "35"},
                        "Line": [_sale_line(30, "Lunch")],
                    }
                )
            ],
        }
        self.deposited = False
        monkeypatch.setattr(
            qbo_import, "_all_inactive_qbo_accounts", lambda cls, client: []
        )
        monkeypatch.setattr(qbo_import, "get_qbo_client", lambda db: object())
        monkeypatch.setattr(
            qbo_import,
            "_all_qbo_objects",
            lambda cls, client: self.sources.get(cls, []),
        )
        monkeypatch.setattr(qbo_ledger_import, "get_qbo_client", lambda db: self._gl())
        # what the import log says of each item it skips or keeps
        self.kept = []
        original, keep = qbo_progress.skipped, qbo_progress.kept
        monkeypatch.setattr(
            qbo_progress,
            "skipped",
            lambda message="": (self.kept.append(message), original(message)),
        )
        monkeypatch.setattr(
            qbo_progress,
            "kept",
            lambda key, message=None: (
                self.kept.append(message) if message else None,
                keep(key, message),
            )[1],
        )

    def _gl(self):
        inv1, inv2 = ("Invoice", "130", "1037"), ("Invoice", "133", "1038")
        pay, sr = ("Payment", "131", ""), ("Sales Receipt", "132", "SR-9")
        sections = {
            "84": [(*inv1, "50"), (*inv2, "40"), (*pay, "-20")],  # A/R
            "79": [(*inv1, "50"), (*inv2, "40"), (*sr, "30")],  # income
            "35": [(*pay, "20"), (*sr, "30")],  # Undeposited Funds
        }
        if self.deposited:
            deposit = ("Deposit", "140", "")
            sections["35"].append((*deposit, "-20"))
            sections["36"] = [(*deposit, "20")]  # Checking
        return LedgerClient(sections)

    def documents(self):
        for step in (
            qbo_import.import_invoices,
            qbo_import.import_payments,
            qbo_import.import_sales_receipts,
        ):
            assert step(self.db)["errors"] == []
        self.db.commit()

    def ledger(self):
        result = qbo_ledger_import.import_ledger(self.db, start=DAY, end=DAY)
        assert result["errors"] == []
        self.db.commit()
        return result

    def invoice(self, number):
        self.db.expire_all()
        return self.db.query(Invoice).filter_by(invoice_number=number).one()

    def balance(self, number):
        account = self.accounts[number].id
        return gl_balances(self.db, [account])[account]

    def postings(self):
        return self.db.query(Transaction).count()


@pytest.fixture
def books(db_session, seed_accounts, monkeypatch):
    return Books(db_session, seed_accounts, monkeypatch)


def test_voiding_a_qbo_invoice_reverses_its_import_posting(client, books):
    books.documents()
    books.ledger()
    assert books.balance("1100") == Decimal("70.00")  # 50 + 40 - 20
    invoice = books.invoice("1038")
    assert invoice.transaction_id is None  # what the import makes
    r = client.post(f"/api/invoices/{invoice.id}/void")
    assert r.status_code == 200, r.text
    assert books.invoice("1038").status == InvoiceStatus.VOID
    assert books.balance("1100") == Decimal("30.00")
    assert books.balance("4000") == Decimal("80.00")  # 50 + 30 still sold
    ledger = books.db.query(QBOMapping).filter_by(qbo_id="Invoice:133").one()
    assert ledger.qbo_sync_token == "changed-in-slowbooks"
    reversal = (
        books.db.query(Transaction).filter_by(source_type="qbo_ledger_void").one()
    )
    assert reversal.source_id == ledger.slowbooks_id


def test_a_later_import_leaves_a_voided_invoice_alone(client, books):
    books.documents()
    books.ledger()
    invoice = books.invoice("1038")
    assert client.post(f"/api/invoices/{invoice.id}/void").status_code == 200
    before = books.postings()
    books.kept.clear()
    books.documents()
    assert books.ledger() == {"imported": 0, "errors": []}
    assert books.postings() == before
    assert books.balance("1100") == Decimal("30.00")
    assert books.kept.count("Changed in SlowBooks; kept as it is here") == 1


def test_a_qbo_invoice_the_ledger_never_posted_just_voids(client, books):
    """As in 2.17 when the ledger import has not run; and when it runs
    later, it does not post the voided invoice."""
    books.documents()
    invoice = books.invoice("1038")
    r = client.post(f"/api/invoices/{invoice.id}/void")
    assert r.status_code == 200, r.text
    assert books.postings() == 0
    books.ledger()
    assert books.balance("1100") == Decimal("30.00")  # 1037's 50 less 20 paid
    assert books.kept.count("Changed in SlowBooks; kept as it is here") == 1


def test_voiding_a_qbo_payment_reverses_its_import_posting(client, books):
    books.documents()
    books.ledger()
    payment = books.db.query(Payment).filter_by(amount=Decimal("20")).one()
    r = client.post(f"/api/payments/{payment.id}/void")
    assert r.status_code == 200, r.text
    assert r.json()["is_voided"] is True
    paid = books.invoice("1037")
    assert (paid.amount_paid, paid.balance_due) == (Decimal("0"), Decimal("50"))
    assert books.balance("1100") == Decimal("90.00")  # both invoices open
    assert books.balance("1200") == Decimal("30.00")  # the receipt's cash only


@pytest.mark.parametrize("ledger_ran", [True, False])
def test_voiding_a_qbo_sales_receipts_payment_voids_the_receipt_too(
    client, books, ledger_ran
):
    """Through the API alone, as the Sales Receipts page does it: the
    receipt is voided with its payment, never left open with nothing in
    A/R (the payment's void reverses the receipt's import posting)."""
    books.documents()
    if ledger_ran:
        books.ledger()
    receipt = books.invoice("SR-9")
    payment = receipt.payment_allocations[0].payment
    r = client.post(f"/api/payments/{payment.id}/void")
    assert r.status_code == 200, r.text
    receipt = books.invoice("SR-9")
    assert receipt.status == InvoiceStatus.VOID
    assert receipt.balance_due == Decimal("0")
    if ledger_ran:
        assert books.balance("4000") == Decimal("90.00")  # the 30.00 sale is gone
        assert books.balance("1200") == Decimal("20.00")  # its cash too
        assert books.balance("1100") == Decimal("70.00")  # A/R untouched


def test_a_qbo_payment_deposited_voids_once_its_deposit_is_voided(client, books):
    """QBO deposited the 20.00: voiding the payment alone would take it out
    of Undeposited Funds a second time. The deposit, brought in from QBO,
    voids here (from the bank register's link, the journal view)."""
    books.deposited = True
    books.documents()
    books.ledger()
    payment = books.db.query(Payment).filter_by(amount=Decimal("20")).one()
    r = client.post(f"/api/payments/{payment.id}/void")
    assert r.status_code == 400
    assert "already been deposited" in r.json()["detail"]
    deposit = (
        books.db.query(QBOMapping).filter_by(qbo_id="Deposit:140").one().slowbooks_id
    )
    assert client.post(f"/api/journal/{deposit}/void").status_code == 200
    assert client.post(f"/api/payments/{payment.id}/void").status_code == 200
    assert books.balance("1200") == Decimal("30.00")
    assert books.balance("1000") == Decimal("0.00")


def test_voiding_a_documents_import_posting_voids_the_document(client, books):
    books.documents()
    books.ledger()
    posting = (
        books.db.query(QBOMapping).filter_by(qbo_id="Invoice:133").one().slowbooks_id
    )
    r = client.post(f"/api/journal/{posting}/void")
    assert r.status_code == 200, r.text
    assert books.invoice("1038").status == InvoiceStatus.VOID
    assert books.balance("1100") == Decimal("30.00")


def test_an_invoice_with_its_own_posting_still_voids_here(
    client, db_session, seed_accounts, seed_customer
):
    """Exported to QBO (so mapped), but written here with its own posting:
    its void reverses that posting as before."""
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": DAY.isoformat(),
            "lines": [{"description": "Consulting", "quantity": 1, "rate": 75}],
        },
    )
    assert r.status_code == 201, r.text
    invoice_id = r.json()["id"]
    db_session.add(
        QBOMapping(entity_type="invoice", qbo_id="555", slowbooks_id=invoice_id)
    )
    db_session.commit()
    r = client.post(f"/api/invoices/{invoice_id}/void")
    assert r.status_code == 200, r.text
    assert (
        db_session.query(Transaction).filter_by(source_type="invoice_void").count() == 1
    )
