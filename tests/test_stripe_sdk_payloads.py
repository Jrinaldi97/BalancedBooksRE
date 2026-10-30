"""Current Stripe SDK objects must work wherever historical dict mocks did."""

from decimal import Decimal
from stripe import StripeObject
from app.services.payments.stripe import StripeProvider


def test_webhook_and_poll_read_actual_sdk_objects(monkeypatch):
    import stripe

    session = StripeObject.construct_from(
        {
            "id": "cs_test",
            "amount_total": 1234,
            "payment_status": "paid",
            "metadata": {"invoice_id": "7"},
        },
        "synthetic",
    )
    event = StripeObject.construct_from(
        {"type": "checkout.session.completed", "data": {"object": session}}, "synthetic"
    )
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *args: event)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", lambda *args: session)
    provider = StripeProvider()
    settings = {"stripe_webhook_secret": "synthetic", "stripe_secret_key": "synthetic"}
    for result in (
        provider.verify_webhook(b"", {}, settings),
        provider.poll_status("cs_test", settings),
    ):
        assert result.amount == Decimal("12.34")
        assert result.invoice_id == 7 and result.status == "paid"
        assert isinstance(result.raw, dict) and isinstance(result.raw["metadata"], dict)
