import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import stripe
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import ValidationError

from books.models import Book
from borrowings.models import Borrowing
from payments.models import ExpiredCheckoutSession, Payment
from payments.services import (
    PaymentProviderUnavailable,
    checkout,
    confirm_session,
    create_rental_payment,
    create_return_fine,
    renew_checkout,
    retrieve_session,
    stripe_client,
)
from users.models import User


class PaymentServiceFixtures(TestCase):
    borrowing: Borrowing
    book: Book

    @classmethod
    def setUpTestData(cls) -> None:
        user = User.objects.create_user("reader@example.com")
        cls.book = Book.objects.create(
            title="The Hobbit", author="J. R. R. Tolkien",
            inventory=3, daily_fee=Decimal("1.50"),
        )
        cls.borrowing = Borrowing.objects.create(
            book=cls.book, user=user, borrow_date=date(2026, 10, 20),
            expected_return_date=date(2026, 10, 23), daily_fee=Decimal("1.50"),
        )

    def setUp(self) -> None:
        # Patch the SDK constructor so no service test can use real Stripe credentials.
        patcher = patch("payments.services.stripe.StripeClient")
        self.client_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.sdk = self.client_factory.return_value


class PaymentCalculationTests(PaymentServiceFixtures):
    def test_rental_uses_planned_days_and_saved_rate(self):
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal("9.00"))
        payment = create_rental_payment(self.borrowing)
        self.assertEqual(payment.money_to_pay, Decimal("4.50"))
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.type, Payment.Type.PAYMENT)
        self.assertEqual(payment.borrowing_id, self.borrowing.pk)
        self.client_factory.assert_not_called()

    def test_same_day_rental_costs_one_day(self):
        self.borrowing.expected_return_date = self.borrowing.borrow_date
        self.assertEqual(create_rental_payment(self.borrowing).money_to_pay,
                         Decimal("1.50"))

    def test_free_rental_is_paid_without_checkout(self):
        self.borrowing.daily_fee = Decimal("0.00")
        payment = create_rental_payment(self.borrowing)
        self.assertEqual(payment.money_to_pay, Decimal("0.00"))
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertIsNone(payment.session_id)
        self.client_factory.assert_not_called()

    def test_rental_rejects_positive_amount_below_minimum_and_above_maximum(self):
        self.borrowing.expected_return_date = self.borrowing.borrow_date
        for fee in (Decimal("0.49"), Decimal("1000000.00")):
            with self.subTest(fee=fee):
                self.borrowing.daily_fee = fee
                with self.assertRaises(ValidationError) as error:
                    create_rental_payment(self.borrowing)
                self.assertIn("expected_return_date", error.exception.detail)
                self.assertFalse(Payment.objects.exists())

    def test_rental_accepts_exact_stripe_amount_boundaries(self):
        self.borrowing.expected_return_date = self.borrowing.borrow_date
        for fee in (Decimal("0.50"), Decimal("999999.99")):
            with self.subTest(fee=fee):
                self.borrowing.daily_fee = fee
                payment = create_rental_payment(self.borrowing)
                self.assertEqual(payment.money_to_pay, fee)
                payment.delete()

    def test_fine_uses_overdue_days_and_double_saved_rate(self):
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal("9.00"))
        payment = create_return_fine(
            self.borrowing, self.borrowing.expected_return_date + timedelta(days=2),
        )
        self.assertEqual(payment.money_to_pay, Decimal("6.00"))
        self.assertEqual(payment.type, Payment.Type.FINE)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.client_factory.assert_not_called()

    def test_early_and_on_time_returns_do_not_create_fines(self):
        for days in (-1, 0):
            with self.subTest(days=days):
                payment = create_return_fine(
                    self.borrowing,
                    self.borrowing.expected_return_date + timedelta(days=days),
                )
                self.assertIsNone(payment)
        self.assertFalse(Payment.objects.exists())

    def test_minimum_nonzero_fine_and_free_rental(self):
        return_date = self.borrowing.expected_return_date + timedelta(days=1)
        self.borrowing.daily_fee = Decimal("0.10")
        fine = create_return_fine(self.borrowing, return_date)
        self.assertEqual(fine.money_to_pay, Decimal("0.50"))
        fine.delete()
        self.borrowing.daily_fee = Decimal("0.00")
        self.assertIsNone(create_return_fine(self.borrowing, return_date))
        self.assertFalse(Payment.objects.exists())

    def test_unknown_legacy_rate_and_excessive_fine_are_rejected(self):
        return_date = self.borrowing.expected_return_date + timedelta(days=1)
        for fee in (None, Decimal("500000.00")):
            with self.subTest(fee=fee):
                self.borrowing.daily_fee = fee
                with self.assertRaises(ValidationError):
                    create_return_fine(self.borrowing, return_date)
                self.assertFalse(Payment.objects.exists())

    def test_duplicate_rental_or_fine_cannot_be_created(self):
        create_rental_payment(self.borrowing)
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_rental_payment(self.borrowing)
        return_date = self.borrowing.expected_return_date + timedelta(days=1)
        create_return_fine(self.borrowing, return_date)
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_return_fine(self.borrowing, return_date)
        self.assertEqual(Payment.objects.count(), 2)


@override_settings(
    STRIPE_SECRET_KEY="sk_test_fake", PAYMENTS_BASE_URL="https://test.local/",
)
class CheckoutServiceTests(PaymentServiceFixtures):
    payment: Payment

    def setUp(self) -> None:
        super().setUp()
        self.payment = create_rental_payment(self.borrowing)
        self.sessions = self.sdk.v1.checkout.sessions
        self.sessions.create.side_effect = self.create_session_response
        self.sessions.retrieve.side_effect = AssertionError("Unexpected session lookup")

    def session_data(self, **overrides) -> dict:
        self.payment.refresh_from_db()
        data = {
            "id": self.payment.session_id or "cs_test_new",
            "url": "https://checkout.stripe.com/c/pay/cs_test_new",
            "metadata": {"payment_id": str(self.payment.pk)},
            "client_reference_id": str(self.payment.checkout_key),
            "amount_total": int(self.payment.money_to_pay * 100),
            "currency": "usd", "mode": "payment",
            "status": "open", "payment_status": "unpaid",
        }
        data.update(overrides)
        return data

    def create_session_response(self, *, params, options):
        # Use an actual SDK object: it exposes to_dict(), not dict.get().
        return stripe.checkout.Session.construct_from(self.session_data(
            client_reference_id=params["client_reference_id"],
        ), "sk_test_fake")

    def existing_session(self, **overrides) -> dict:
        self.payment.session_id = "cs_test_existing"
        self.payment.session_url = "https://checkout.stripe.com/c/pay/cs_test_existing"
        self.payment.save(update_fields=["session_id", "session_url"])
        data = self.session_data(**overrides)
        self.sessions.retrieve.side_effect = None
        self.sessions.retrieve.return_value = stripe.checkout.Session.construct_from(
            data, "sk_test_fake",
        )
        return data

    def test_checkout_sends_amount_metadata_urls_and_stable_idempotency_key(self):
        key = str(self.payment.checkout_key)
        result = checkout(self.payment)
        call = self.sessions.create.call_args.kwargs
        params = call["params"]
        self.assertEqual(call["options"], {"idempotency_key": key})
        self.assertEqual(params["client_reference_id"], key)
        self.assertEqual(params["metadata"], {"payment_id": str(self.payment.pk)})
        self.assertEqual(params["mode"], "payment")
        self.assertEqual(params["success_url"],
                         "https://test.local" + reverse("payments:success")
                         + "?session_id={CHECKOUT_SESSION_ID}")
        self.assertEqual(params["cancel_url"],
                         "https://test.local" + reverse("payments:cancel"))
        item = params["line_items"][0]
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["price_data"]["currency"], "usd")
        self.assertEqual(item["price_data"]["unit_amount"], 450)
        self.assertEqual(result.session_id, "cs_test_new")
        self.assertTrue(result.session_url)
        self.assertEqual(result.status, Payment.Status.PENDING)
        self.assertEqual(Payment.objects.count(), 1)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)

    def test_fine_checkout_has_fine_description(self):
        self.payment.type = Payment.Type.FINE
        self.payment.save(update_fields=["type"])
        checkout(self.payment)
        item = self.sessions.create.call_args.kwargs["params"]["line_items"][0]
        self.assertEqual(item["price_data"]["product_data"]["name"],
                         f"Library overdue fine #{self.payment.pk}")

    def test_repeated_checkout_reuses_existing_open_session(self):
        data = self.existing_session()
        for _ in range(2):
            result = checkout(self.payment)
            self.assertEqual(result.session_id, data["id"])
            self.assertEqual(result.status, Payment.Status.PENDING)
        self.sessions.create.assert_not_called()
        self.assertEqual(Payment.objects.count(), 1)

    def test_paid_payment_does_not_contact_stripe_even_with_stale_instance(self):
        Payment.objects.filter(pk=self.payment.pk).update(status=Payment.Status.PAID)
        self.assertEqual(checkout(self.payment).status, Payment.Status.PAID)
        self.client_factory.assert_not_called()

    def test_checkout_confirms_completed_session(self):
        self.existing_session(status="complete", payment_status="paid")
        self.assertEqual(checkout(self.payment).status, Payment.Status.PAID)
        self.sessions.create.assert_not_called()

    def test_expired_checkout_marks_payment_and_requires_renewal(self):
        self.existing_session(status="expired")
        with self.assertRaises(ValidationError):
            checkout(self.payment)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.EXPIRED)
        self.sessions.create.assert_not_called()

    def test_checkout_rejects_amount_below_minimum_without_stripe_call(self):
        self.payment.money_to_pay = Decimal("0.49")
        self.payment.save(update_fields=["money_to_pay"])
        with self.assertRaises(ValidationError):
            checkout(self.payment)
        self.client_factory.assert_not_called()

    def test_provider_failure_preserves_key_and_retry_uses_same_key(self):
        key = self.payment.checkout_key
        self.sessions.create.side_effect = stripe.APIConnectionError("Connection lost")
        with self.assertRaises(PaymentProviderUnavailable) as error:
            checkout(self.payment)
        self.assertEqual(error.exception.status_code, 503)
        self.payment.refresh_from_db()
        self.assertIsNone(self.payment.session_id)
        self.assertEqual(self.payment.checkout_key, key)
        first_options = self.sessions.create.call_args.kwargs["options"]
        self.sessions.create.side_effect = self.create_session_response
        checkout(self.payment)
        self.assertEqual(
            self.sessions.create.call_args.kwargs["options"], first_options,
        )
        self.assertEqual(Payment.objects.count(), 1)

    def test_retrieve_converts_sdk_object_to_dictionary(self):
        data = self.existing_session()
        self.assertEqual(retrieve_session(data["id"]), data)
        self.sessions.retrieve.assert_called_once_with(data["id"])

    def test_retrieve_maps_invalid_session_and_provider_errors(self):
        cases = (
            (stripe.InvalidRequestError("Missing session", "id"), ValidationError),
            (stripe.APIConnectionError("Connection lost"), PaymentProviderUnavailable),
        )
        for provider_error, expected in cases:
            with self.subTest(error=type(provider_error).__name__):
                self.sessions.retrieve.side_effect = provider_error
                with self.assertRaises(expected):
                    retrieve_session("cs_test_unknown")

    def test_confirmation_is_idempotent_and_recovers_missing_session(self):
        data = self.session_data(status="complete", payment_status="paid")
        for _ in range(2):
            result = confirm_session(data)
            self.assertEqual(result.status, Payment.Status.PAID)
            self.assertEqual(result.session_id, data["id"])
            self.assertEqual(result.session_url, data["url"])
        self.assertEqual(Payment.objects.count(), 1)
        self.client_factory.assert_not_called()

    def test_confirmation_rejects_missing_invalid_and_unknown_payment_metadata(self):
        for metadata in ({}, {"payment_id": "abc"}, {"payment_id": "999999"}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValidationError):
                    confirm_session(self.session_data(metadata=metadata))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertIsNone(self.payment.session_id)

    def test_confirmation_rejects_mismatched_session_fields(self):
        self.existing_session()
        for changes in (
            {"id": None}, {"id": "cs_test_other"},
            {"client_reference_id": str(uuid.uuid4())},
            {"amount_total": 1}, {"currency": "eur"}, {"mode": "subscription"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    confirm_session(self.session_data(
                        status="complete", payment_status="paid", **changes,
                    ))
                self.payment.refresh_from_db()
                self.assertEqual(self.payment.status, Payment.Status.PENDING)
                self.assertEqual(self.payment.session_id, "cs_test_existing")

    def test_complete_but_unpaid_session_does_not_mark_payment_paid(self):
        result = confirm_session(self.session_data(status="complete"))
        self.assertEqual(result.status, Payment.Status.PENDING)

    def test_no_payment_required_only_confirms_zero_amount(self):
        data = self.session_data(
            status="complete", payment_status="no_payment_required",
        )
        self.assertEqual(confirm_session(data).status, Payment.Status.PENDING)
        self.payment.money_to_pay = Decimal("0.00")
        self.payment.save(update_fields=["money_to_pay"])
        data["amount_total"] = 0
        self.assertEqual(confirm_session(data).status, Payment.Status.PAID)

    def test_late_expiration_cannot_downgrade_paid_payment(self):
        data = self.session_data(status="complete", payment_status="paid")
        confirm_session(data)
        data.update(status="expired", payment_status="unpaid")
        self.assertEqual(confirm_session(data).status, Payment.Status.PAID)

    def test_renew_replaces_expired_session_and_preserves_payment_and_inventory(self):
        old = self.existing_session(status="expired")
        old_key = self.payment.checkout_key
        result = renew_checkout(self.payment)
        self.assertEqual(result.pk, self.payment.pk)
        self.assertEqual(result.status, Payment.Status.PENDING)
        self.assertNotEqual(result.session_id, old["id"])
        self.assertNotEqual(result.checkout_key, old_key)
        self.assertEqual(result.money_to_pay, Decimal("4.50"))
        archived = result.expired_sessions.get()
        self.assertEqual(archived.session_id, old["id"])
        self.assertEqual(archived.checkout_key, old_key)
        self.assertEqual(Payment.objects.count(), 1)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)

    def test_renew_rejects_open_or_complete_unpaid_session(self):
        for status in ("open", "complete"):
            with self.subTest(status=status):
                self.existing_session(status=status)
                with self.assertRaises(ValidationError):
                    renew_checkout(self.payment)
                self.sessions.create.assert_not_called()
                self.assertFalse(ExpiredCheckoutSession.objects.exists())

    def test_renew_rejects_paid_payment_without_contacting_stripe(self):
        self.payment.status = Payment.Status.PAID
        self.payment.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            renew_checkout(self.payment)
        self.client_factory.assert_not_called()

    def test_renew_detects_payment_completed_at_stripe(self):
        self.existing_session(status="complete", payment_status="paid")
        self.assertEqual(renew_checkout(self.payment).status, Payment.Status.PAID)
        self.sessions.create.assert_not_called()
        self.assertFalse(ExpiredCheckoutSession.objects.exists())

    def test_failed_renewal_can_resume_without_rotating_key_again(self):
        self.existing_session(status="expired")
        old_key = self.payment.checkout_key
        self.sessions.create.side_effect = stripe.APIConnectionError("Connection lost")
        with self.assertRaises(PaymentProviderUnavailable):
            renew_checkout(self.payment)
        self.payment.refresh_from_db()
        new_key = self.payment.checkout_key
        self.assertNotEqual(new_key, old_key)
        self.assertIsNone(self.payment.session_id)
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.sessions.create.side_effect = self.create_session_response
        result = renew_checkout(self.payment)
        self.assertEqual(result.checkout_key, new_key)
        self.assertEqual(result.session_id, "cs_test_new")
        self.assertEqual(result.expired_sessions.count(), 1)
        self.assertEqual(self.sessions.retrieve.call_count, 1)

    def test_old_expired_callback_does_not_expire_renewed_or_paid_session(self):
        old = self.existing_session(status="expired")
        renewed = renew_checkout(self.payment)
        new_id = renewed.session_id
        self.assertEqual(confirm_session(old).status, Payment.Status.PENDING)
        confirm_session(self.session_data(status="complete", payment_status="paid"))
        result = confirm_session(old)
        self.assertEqual(result.status, Payment.Status.PAID)
        self.assertEqual(result.session_id, new_id)
        self.assertEqual(result.expired_sessions.count(), 1)


class StripeConfigurationTests(SimpleTestCase):
    @override_settings(STRIPE_SECRET_KEY="")
    def test_missing_secret_returns_service_unavailable(self):
        with patch("payments.services.stripe.StripeClient") as factory:
            with self.assertRaises(PaymentProviderUnavailable) as error:
                stripe_client()
            self.assertEqual(error.exception.status_code, 503)
            factory.assert_not_called()
