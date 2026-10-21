import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from kombu.exceptions import OperationalError
from rest_framework.exceptions import ValidationError

from books.models import Book
from borrowings.models import Borrowing
from notifications.tasks import send_telegram_notification
from payments.models import ExpiredCheckoutSession, Payment
from payments.services import confirm_session
from users.models import User


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SuccessfulPaymentNotificationTests(TestCase):
    user: User
    book: Book
    borrowing: Borrowing
    payment: Payment

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user("reader@example.com")
        cls.book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=2,
            daily_fee=Decimal("1.50"),
        )
        cls.borrowing = Borrowing.objects.create(
            user=cls.user,
            book=cls.book,
            expected_return_date=timezone.localdate() + timedelta(days=3),
            daily_fee=cls.book.daily_fee,
        )
        cls.payment = Payment.objects.create(
            borrowing=cls.borrowing,
            money_to_pay=Decimal("4.50"),
            session_id="cs_test_notification",
        )

    def setUp(self) -> None:
        enqueue = patch.object(send_telegram_notification, "delay")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)

        for target in (
            "requests.sessions.Session.request",
            "payments.services.stripe.StripeClient",
        ):
            network_guard = patch(
                target,
                side_effect=AssertionError("These tests must not contact providers"),
            )
            network_guard.start()
            self.addCleanup(network_guard.stop)

    def session_data(self, **changes) -> dict:
        data = {
            "id": self.payment.session_id,
            "metadata": {"payment_id": str(self.payment.pk)},
            "client_reference_id": str(self.payment.checkout_key),
            "amount_total": 450,
            "currency": "usd",
            "mode": "payment",
            "status": "complete",
            "payment_status": "paid",
        }
        data.update(changes)
        return data

    def assert_payment_message(self, payment_type: str) -> None:
        self.enqueue.assert_called_once()
        text = self.enqueue.call_args.args[0]
        self.assertIsInstance(text, str)
        for detail in (
            f"Payment #{self.payment.pk}",
            f"Borrowing #{self.borrowing.pk}",
            self.user.email,
            self.book.title,
            "4.50",
            "USD",
            payment_type,
        ):
            with self.subTest(detail=detail):
                self.assertIn(detail, text)

    def test_rental_payment_enqueues_message_with_payment_details(self):
        with self.captureOnCommitCallbacks(execute=True):
            result = confirm_session(self.session_data())

        self.assertEqual(result.status, Payment.Status.PAID)
        self.assert_payment_message(Payment.Type.PAYMENT)

    def test_fine_payment_enqueues_message_identifying_fine(self):
        Payment.objects.filter(pk=self.payment.pk).update(type=Payment.Type.FINE)

        with self.captureOnCommitCallbacks(execute=True):
            result = confirm_session(self.session_data())

        self.assertEqual(result.status, Payment.Status.PAID)
        self.assert_payment_message(Payment.Type.FINE)

    def test_broker_failure_does_not_undo_confirmed_payment(self):
        self.enqueue.side_effect = OperationalError("Broker unavailable")

        with self.assertLogs(level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                result = confirm_session(self.session_data())

        self.assertEqual(result.status, Payment.Status.PAID)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PAID)
        self.enqueue.assert_called_once()

    def test_notification_waits_for_transaction_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                result = confirm_session(self.session_data())
                self.assertEqual(result.status, Payment.Status.PAID)
                self.enqueue.assert_not_called()
            self.enqueue.assert_not_called()

        self.enqueue.assert_called_once()

    def test_repeated_confirmations_enqueue_only_one_notification(self):
        for _ in range(3):
            with self.captureOnCommitCallbacks(execute=True):
                result = confirm_session(self.session_data())
            self.assertEqual(result.status, Payment.Status.PAID)

        self.enqueue.assert_called_once()

    def test_repeated_confirmations_inside_one_transaction_enqueue_once(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                confirm_session(self.session_data())
                confirm_session(self.session_data())
                self.enqueue.assert_not_called()

        self.enqueue.assert_called_once()

    def test_rollback_discards_notification_and_allows_later_confirmation(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                confirm_session(self.session_data())
                transaction.set_rollback(True)

        self.enqueue.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

        with self.captureOnCommitCallbacks(execute=True):
            confirm_session(self.session_data())

        self.enqueue.assert_called_once()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PAID)

    def test_unpaid_or_expired_session_does_not_enqueue_notification(self):
        for session_status in ("open", "complete", "expired"):
            with self.subTest(session_status=session_status):
                Payment.objects.filter(pk=self.payment.pk).update(
                    status=Payment.Status.PENDING
                )
                with self.captureOnCommitCallbacks(execute=True):
                    result = confirm_session(
                        self.session_data(
                            status=session_status, payment_status="unpaid"
                        )
                    )

                self.assertNotEqual(result.status, Payment.Status.PAID)
                self.enqueue.assert_not_called()

    def test_mismatched_session_does_not_enqueue_notification(self):
        for changes in (
            {"amount_total": 999},
            {"currency": "eur"},
            {"client_reference_id": str(uuid.uuid4())},
            {"id": "cs_other_payment"},
        ):
            with self.subTest(changes=changes):
                with self.captureOnCommitCallbacks(execute=True):
                    with self.assertRaises(ValidationError):
                        confirm_session(self.session_data(**changes))

                self.enqueue.assert_not_called()
                self.payment.refresh_from_db()
                self.assertEqual(self.payment.status, Payment.Status.PENDING)

    def test_late_expiration_after_payment_does_not_repeat_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            confirm_session(self.session_data())
        self.enqueue.assert_called_once()
        self.enqueue.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            result = confirm_session(
                self.session_data(status="expired", payment_status="unpaid")
            )

        self.assertEqual(result.status, Payment.Status.PAID)
        self.enqueue.assert_not_called()

    def test_paid_archived_session_enqueues_once_across_different_sessions(self):
        old_key = uuid.uuid4()
        ExpiredCheckoutSession.objects.create(
            payment=self.payment,
            session_id="cs_test_archived",
            checkout_key=old_key,
        )
        old_session = self.session_data(
            id="cs_test_archived", client_reference_id=str(old_key)
        )

        for session in (old_session, self.session_data(), old_session):
            with self.captureOnCommitCallbacks(execute=True):
                result = confirm_session(session)
            self.assertEqual(result.status, Payment.Status.PAID)

        self.enqueue.assert_called_once()
