from io import StringIO
from unittest.mock import call, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from books.models import Book
from borrowings.models import Borrowing
from payments.models import Payment
from payments.services import PaymentProviderUnavailable
from payments.tasks import sync_payment_sessions
from users.models import User


class PaymentSyncTests(TestCase):
    book: Book
    user: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.book = Book.objects.create(
            title="The Hobbit", author="J. R. R. Tolkien",
            inventory=3, daily_fee="1.50",
        )
        cls.user = User.objects.create_user("reader@example.com")

    def setUp(self) -> None:
        patcher = patch("payments.tasks.retrieve_session")
        self.retrieve = patcher.start()
        self.addCleanup(patcher.stop)
        self.retrieve.side_effect = AssertionError("Unexpected Stripe session lookup")

        network_guard = patch(
            "payments.services.stripe.StripeClient",
            side_effect=AssertionError("These tests must not contact Stripe"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def make_payment(
        self, session_id: str | None, *, status: str = Payment.Status.PENDING,
        payment_type: str = Payment.Type.PAYMENT,
    ) -> Payment:
        borrowing = Borrowing.objects.create(
            book=self.book, user=self.user, expected_return_date=timezone.localdate(),
            daily_fee="1.50",
        )
        return Payment.objects.create(
            borrowing=borrowing, money_to_pay="1.50", session_id=session_id,
            status=status, type=payment_type,
        )

    @staticmethod
    def session_data(
        payment: Payment, *, status: str = "open", payment_status: str = "unpaid",
    ) -> dict:
        return {
            "id": payment.session_id,
            "metadata": {"payment_id": str(payment.pk)},
            "client_reference_id": str(payment.checkout_key),
            "amount_total": 150, "currency": "usd", "mode": "payment",
            "status": status, "payment_status": payment_status,
        }

    def test_empty_database_requires_no_stripe_requests(self):
        result = sync_payment_sessions.run()
        self.assertEqual(result, {"checked": 0, "failed": 0})
        self.retrieve.assert_not_called()

    def test_only_pending_payments_with_sessions_are_checked(self):
        pending = self.make_payment("cs_test_pending")
        no_session = self.make_payment(None)
        paid = self.make_payment("cs_test_paid", status=Payment.Status.PAID)
        expired = self.make_payment("cs_test_expired", status=Payment.Status.EXPIRED)
        self.retrieve.side_effect = None
        self.retrieve.return_value = self.session_data(pending)

        self.assertEqual(sync_payment_sessions.run(), {"checked": 1, "failed": 0})
        self.retrieve.assert_called_once_with("cs_test_pending")
        for payment, expected in (
            (pending, Payment.Status.PENDING), (no_session, Payment.Status.PENDING),
            (paid, Payment.Status.PAID), (expired, Payment.Status.EXPIRED),
        ):
            with self.subTest(payment_id=payment.pk):
                payment.refresh_from_db()
                self.assertEqual(payment.status, expected)

    def test_completed_rental_and_expired_fine_are_synchronized(self):
        rental = self.make_payment("cs_test_rental")
        fine = self.make_payment("cs_test_fine", payment_type=Payment.Type.FINE)
        sessions = {
            rental.session_id: self.session_data(
                rental, status="complete", payment_status="paid",
            ),
            fine.session_id: self.session_data(fine, status="expired"),
        }
        self.retrieve.side_effect = sessions.__getitem__

        self.assertEqual(sync_payment_sessions.run(), {"checked": 2, "failed": 0})
        rental.refresh_from_db()
        fine.refresh_from_db()
        self.assertEqual(rental.status, Payment.Status.PAID)
        self.assertEqual(fine.status, Payment.Status.EXPIRED)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)
        self.assertEqual(Payment.objects.count(), 2)

    def test_complete_but_unpaid_session_remains_pending(self):
        payment = self.make_payment("cs_test_waiting")
        self.retrieve.side_effect = None
        self.retrieve.return_value = self.session_data(payment, status="complete")

        self.assertEqual(sync_payment_sessions.run(), {"checked": 1, "failed": 0})
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_retrieval_errors_are_logged_and_do_not_stop_remaining_payments(self):
        successful = self.make_payment("cs_test_success")
        invalid = self.make_payment("cs_test_invalid")
        unavailable = self.make_payment("cs_test_unavailable")
        # Payments are processed newest first; both failures precede the valid session.
        self.retrieve.side_effect = [
            PaymentProviderUnavailable(), ValidationError("Unknown Stripe session"),
            self.session_data(successful, status="complete", payment_status="paid"),
        ]
        with self.assertLogs("payments.tasks", level="WARNING") as logs:
            result = sync_payment_sessions.run()

        self.assertEqual(result, {"checked": 1, "failed": 2})
        self.assertEqual(self.retrieve.call_args_list, [
            call(unavailable.session_id), call(invalid.session_id),
            call(successful.session_id),
        ])
        self.assertEqual(len(logs.output), 2)
        self.assertIn(str(unavailable.pk), logs.output[0])
        self.assertIn(str(invalid.pk), logs.output[1])
        for payment, expected in (
            (successful, Payment.Status.PAID), (invalid, Payment.Status.PENDING),
            (unavailable, Payment.Status.PENDING),
        ):
            payment.refresh_from_db()
            self.assertEqual(payment.status, expected)

    def test_confirmation_error_does_not_mark_payment_paid_or_stop_processing(self):
        valid = self.make_payment("cs_test_valid")
        invalid = self.make_payment("cs_test_wrong_amount")
        wrong_session = self.session_data(
            invalid, status="complete", payment_status="paid",
        )
        wrong_session["amount_total"] = 1
        self.retrieve.side_effect = [
            wrong_session,
            self.session_data(valid, status="complete", payment_status="paid"),
        ]
        with self.assertLogs("payments.tasks", level="WARNING"):
            result = sync_payment_sessions.run()

        self.assertEqual(result, {"checked": 1, "failed": 1})
        invalid.refresh_from_db()
        valid.refresh_from_db()
        self.assertEqual(invalid.status, Payment.Status.PENDING)
        self.assertEqual(valid.status, Payment.Status.PAID)

    def test_next_run_retries_failed_payment_but_skips_already_paid_payment(self):
        paid_later = self.make_payment("cs_test_retry")
        paid_now = self.make_payment("cs_test_paid_now")
        self.retrieve.side_effect = [
            self.session_data(paid_now, status="complete", payment_status="paid"),
            PaymentProviderUnavailable(),
        ]
        with self.assertLogs("payments.tasks", level="WARNING"):
            self.assertEqual(sync_payment_sessions.run(), {"checked": 1, "failed": 1})

        self.retrieve.reset_mock()
        self.retrieve.side_effect = None
        self.retrieve.return_value = self.session_data(
            paid_later, status="complete", payment_status="paid",
        )
        self.assertEqual(sync_payment_sessions.run(), {"checked": 1, "failed": 0})
        self.retrieve.assert_called_once_with("cs_test_retry")
        self.assertEqual(Payment.objects.filter(status=Payment.Status.PAID).count(), 2)

    def test_command_synchronizes_payment_and_prints_summary_without_worker(self):
        payment = self.make_payment("cs_test_command")
        self.retrieve.side_effect = None
        self.retrieve.return_value = self.session_data(payment, status="expired")
        output = StringIO()

        call_command("sync_payments", stdout=output)

        self.assertIn("Checked: 1; failed: 0", output.getvalue())
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.EXPIRED)
        self.retrieve.assert_called_once_with("cs_test_command")

    def test_command_succeeds_when_nothing_needs_synchronization(self):
        self.make_payment(None)
        output = StringIO()
        call_command("sync_payments", stdout=output)
        self.assertIn("Checked: 0; failed: 0", output.getvalue())
        self.retrieve.assert_not_called()

    def test_command_reports_failure_but_keeps_successful_updates(self):
        failed = self.make_payment("cs_test_failed")
        successful = self.make_payment("cs_test_successful")
        self.retrieve.side_effect = [
            self.session_data(successful, status="complete", payment_status="paid"),
            PaymentProviderUnavailable(),
        ]
        output = StringIO()
        with self.assertLogs("payments.tasks", level="WARNING"):
            with self.assertRaises(CommandError):
                call_command("sync_payments", stdout=output)

        self.assertIn("Checked: 1; failed: 1", output.getvalue())
        failed.refresh_from_db()
        successful.refresh_from_db()
        self.assertEqual(failed.status, Payment.Status.PENDING)
        self.assertEqual(successful.status, Payment.Status.PAID)
