from decimal import Decimal

import stripe
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from rest_framework.exceptions import APIException, ValidationError

from payments.models import Payment


class PaymentProviderUnavailable(APIException):
    status_code = 503
    default_detail = "Stripe is unavailable or not configured. Retry checkout later."


def stripe_client():
    if not settings.STRIPE_SECRET_KEY:
        raise PaymentProviderUnavailable()

    return stripe.StripeClient(
        settings.STRIPE_SECRET_KEY,
        http_client=stripe.RequestsClient(timeout=10),
        max_network_retries=1
    )


def create_rental_payment(borrowing):
    days = max((borrowing.expected_return_date - borrowing.borrow_date).days, 1)
    amount = borrowing.daily_fee * days

    if amount > Decimal("999999.99"):
        raise ValidationError({"expected_return_date": "Rental amount is too large"})

    if Decimal("0") < amount < Decimal("0.50"):
        raise ValidationError({
            "expected_return_date": (
                "The rental total must be zero or at least 0.50 USD"
            )
        })

    return Payment.objects.create(
        borrowing=borrowing,
        money_to_pay=amount,
        status=Payment.Status.PAID if amount == 0 else Payment.Status.PENDING
    )


def confirm_session(session):
    payment_id = session.get("metadata", {}).get("payment_id")

    if not payment_id or not str(payment_id).isdigit():
        raise ValidationError("Session does not identify a library payment.")

    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(pk=payment_id).first()
        if payment is None:
            raise ValidationError("Unknown payment.")

        if (
            session.get("client_reference_id") != str(payment.checkout_key)
            or session.get("amount_total") != int(payment.money_to_pay * 100)
            or session.get("currency") != "usd"
            or session.get("mode") != "payment"
            or (payment.session_id and payment.session_id != session.get("id"))
        ):
            raise ValidationError("Stripe session does not match the payment.")

        paid = session.get("payment_status") == "paid"
        free = (
            payment.money_to_pay == 0
            and session.get("payment_status") == "no_payment_required"
        )

        if session.get("status") == "complete" and (paid or free):
            Payment.objects.filter(pk=payment.pk, status=Payment.Status.PENDING).update(
                status=Payment.Status.PAID, session_id=session["id"],
            )

        payment.refresh_from_db()

        return payment


def retrieve_session(session_id):
    try:
        session = stripe_client().v1.checkout.sessions.retrieve(session_id)
        return session.to_dict()

    except stripe.InvalidRequestError:
        raise ValidationError("Unknown or invalid Stripe session")

    except stripe.StripeError:
        raise PaymentProviderUnavailable()


def checkout(payment):
    if payment.status == Payment.Status.PAID:
        return payment

    if payment.session_id:
        session = retrieve_session(payment.session_id)
        payment = confirm_session(session)

        if session.get("status") == "expired":
            raise ValidationError("Checkout session expired. Contact an administrator.")

        return payment

    if payment.money_to_pay < Decimal("0.50"):
        raise ValidationError("Stripe requires at least 0.50 USD for this payment.")

    base = settings.PAYMENTS_BASE_URL.rstrip("/")

    try:
        session = stripe_client().v1.checkout.sessions.create(
            params={
                "mode": "payment",
                "client_reference_id": str(payment.checkout_key),
                "metadata": {"payment_id": str(payment.pk)},
                "success_url": (
                    base + reverse("payments:success")
                    + "?session_id={CHECKOUT_SESSION_ID}"
                ),
                "cancel_url": base + reverse("payments:cancel"),
                "line_items": [{
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(payment.money_to_pay * 100),
                        "product_data": {
                            "name": f"Library rental payment #{payment.pk}"
                        },
                    },
                    "quantity": 1,
                }],
            },
            options={"idempotency_key": str(payment.checkout_key)},
        )

    except stripe.StripeError:
        raise PaymentProviderUnavailable()

    Payment.objects.filter(pk=payment.pk).update(
        session_id=session.id, session_url=session.url or "",
    )
    payment.refresh_from_db()

    return payment
