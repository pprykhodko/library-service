import uuid
from decimal import Decimal

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from rest_framework.exceptions import APIException, ValidationError

from notifications.tasks import send_telegram_notification
from payments.models import ExpiredCheckoutSession, Payment


class PaymentProviderUnavailable(APIException):
    status_code = 503
    default_detail = "Stripe is unavailable or not configured. Retry checkout later."


def stripe_client():
    if not settings.STRIPE_SECRET_KEY:
        raise PaymentProviderUnavailable()

    return stripe.StripeClient(
        settings.STRIPE_SECRET_KEY,
        http_client=stripe.RequestsClient(timeout=10),
        max_network_retries=1,
    )


def create_rental_payment(borrowing):
    days = max((borrowing.expected_return_date - borrowing.borrow_date).days, 1)
    amount = borrowing.daily_fee * days

    if amount > Decimal("999999.99"):
        raise ValidationError({"expected_return_date": "Rental amount is too large"})

    if Decimal("0") < amount < Decimal("0.50"):
        raise ValidationError(
            {
                "expected_return_date": (
                    "The rental total must be zero or at least 0.50 USD"
                )
            }
        )

    return Payment.objects.create(
        borrowing=borrowing,
        money_to_pay=amount,
        status=Payment.Status.PAID if amount == 0 else Payment.Status.PENDING,
    )


def lock_borrower(user_id):
    get_user_model().objects.filter(pk=user_id).update(last_login=F("last_login"))


def create_return_fine(borrowing, return_date):
    days = (return_date - borrowing.expected_return_date).days
    if days <= 0:
        return None

    if borrowing.daily_fee is None:
        raise ValidationError(
            {
                "daily_fee": (
                    "Restore this legacy borrowing's original daily fee "
                    "before returning it. The historical rate is unknown."
                )
            }
        )

    amount = borrowing.daily_fee * days * Decimal("2")

    if amount == 0:
        return None

    amount = max(amount, Decimal("0.50"))

    if amount > Decimal("999999.99"):
        raise ValidationError("The fine exceeds Stripe's maximum payment amount")

    return Payment.objects.create(
        borrowing=borrowing,
        type=Payment.Type.FINE,
        money_to_pay=amount,
    )


def confirm_session(session):
    payment_id = session.get("metadata", {}).get("payment_id")

    if not payment_id or not str(payment_id).isdigit():
        raise ValidationError("Session does not identify a library payment.")

    with transaction.atomic():
        Payment.objects.filter(pk=payment_id).update(status=F("status"))
        payment = Payment.objects.select_for_update().filter(pk=payment_id).first()

        if payment is None:
            raise ValidationError("Unknown payment.")

        historical = payment.expired_sessions.filter(
            session_id=session.get("id")
        ).first()
        expected_key = historical.checkout_key if historical else payment.checkout_key
        if (
            not session.get("id")
            or session.get("client_reference_id") != str(expected_key)
            or session.get("amount_total") != int(payment.money_to_pay * 100)
            or session.get("currency") != "usd"
            or session.get("mode") != "payment"
            or (
                not historical
                and payment.session_id
                and payment.session_id != session.get("id")
            )
        ):
            raise ValidationError("Stripe session does not match the payment.")

        if not historical and not payment.session_id:
            Payment.objects.filter(pk=payment.pk).update(
                session_id=session["id"], session_url=session.get("url") or ""
            )

        paid = session.get("payment_status") == "paid"
        free = (
            payment.money_to_pay == 0
            and session.get("payment_status") == "no_payment_required"
        )

        if session.get("status") == "complete" and (paid or free):
            updated = (
                Payment.objects.filter(pk=payment.pk)
                .exclude(status=Payment.Status.PAID)
                .update(status=Payment.Status.PAID)
            )
            if updated:
                borrowing = payment.borrowing
                message = (
                    f"Successful payment\n"
                    f"Payment #{payment.pk}\n"
                    f"Type: {payment.type}\n"
                    f"Amount: {payment.money_to_pay:.2f} USD\n"
                    f"Borrowing #{borrowing.pk}\n"
                    f"Book: {borrowing.book.title}\n"
                    f"Reader: {borrowing.user.email}"
                )
                transaction.on_commit(
                    lambda: send_telegram_notification.delay(message), robust=True
                )

        elif session.get("status") == "expired" and not historical:
            Payment.objects.filter(pk=payment.pk, status=Payment.Status.PENDING).update(
                status=Payment.Status.EXPIRED
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
    payment.refresh_from_db()

    if payment.status == Payment.Status.PAID:
        return payment

    if payment.session_id:
        session = retrieve_session(payment.session_id)
        payment = confirm_session(session)

        if payment.status == Payment.Status.EXPIRED:
            raise ValidationError("Checkout session expired. Use the renew endpoint.")

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
                    base
                    + reverse("payments:success")
                    + "?session_id={CHECKOUT_SESSION_ID}"
                ),
                "cancel_url": base + reverse("payments:cancel"),
                "line_items": [
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": int(payment.money_to_pay * 100),
                            "product_data": {
                                "name": (
                                    f"Library rental payment #{payment.pk}"
                                    if payment.type == Payment.Type.PAYMENT
                                    else f"Library overdue fine #{payment.pk}"
                                )
                            },
                        },
                        "quantity": 1,
                    }
                ],
            },
            options={"idempotency_key": str(payment.checkout_key)},
        )

    except stripe.StripeError:
        raise PaymentProviderUnavailable()

    Payment.objects.filter(pk=payment.pk, checkout_key=payment.checkout_key).update(
        session_id=session.id, session_url=session.url or ""
    )
    return confirm_session(session.to_dict())


def renew_checkout(payment):
    payment.refresh_from_db()

    if payment.status == Payment.Status.PAID:
        raise ValidationError("A paid payment cannot be renewed")

    if not payment.session_id:
        return checkout(payment)

    previous_key = payment.checkout_key
    session = retrieve_session(payment.session_id)
    payment = confirm_session(session)

    if payment.status == Payment.Status.PAID:
        return payment

    if session.get("status") != "expired":
        raise ValidationError("Only expired sessions can be renewed. Reuse checkout.")

    with transaction.atomic():
        updated = Payment.objects.filter(
            pk=payment.pk, checkout_key=previous_key, status=Payment.Status.EXPIRED
        ).update(
            checkout_key=uuid.uuid4(),
            session_id=None,
            session_url="",
            status=Payment.Status.PENDING,
        )
        if updated:
            ExpiredCheckoutSession.objects.create(
                payment=payment, session_id=session["id"], checkout_key=previous_key
            )

    return checkout(payment)
