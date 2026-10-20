from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from books.models import Book
from borrowings.models import Borrowing
from notifications.tasks import send_telegram_notification
from payments.models import Payment
from users.models import User

BORROWINGS_URL = reverse("borrowings:borrowings-list")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class NewBorrowingNotificationTests(APITestCase):
    client: APIClient
    user: User
    book: Book

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user("reader@example.com")
        cls.book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=3,
            daily_fee=Decimal("1.50"),
        )

    def setUp(self) -> None:
        self.client.force_authenticate(self.user)
        self.today = timezone.localdate()
        self.expected_return_date = self.today + timedelta(days=3)
        self.payload = {
            "book": self.book.pk,
            "expected_return_date": self.expected_return_date.isoformat(),
        }
        enqueue = patch.object(send_telegram_notification, "delay")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)

        network_guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("The API must not send Telegram HTTP requests"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def test_created_borrowing_enqueues_one_message_with_borrowing_details(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(BORROWINGS_URL, self.payload)

        self.assertEqual(response.status_code, 201)
        borrowing = Borrowing.objects.get(pk=response.data["id"])
        self.assertTrue(Payment.objects.filter(borrowing=borrowing).exists())
        self.enqueue.assert_called_once()
        text = self.enqueue.call_args.args[0]
        self.assertIsInstance(text, str)
        for detail in (
            f"Borrowing #{borrowing.pk}",
            self.book.title,
            self.user.email,
            self.today.isoformat(),
            self.expected_return_date.isoformat(),
        ):
            with self.subTest(detail=detail):
                self.assertIn(detail, text)

    def test_message_is_not_enqueued_before_transaction_commits(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                response = self.client.post(BORROWINGS_URL, self.payload)
                self.assertEqual(response.status_code, 201)
                self.assertTrue(
                    Borrowing.objects.filter(pk=response.data["id"]).exists()
                )
                self.enqueue.assert_not_called()
            self.enqueue.assert_not_called()

        self.enqueue.assert_called_once()

    def test_outer_transaction_rollback_discards_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                response = self.client.post(BORROWINGS_URL, self.payload)
                self.assertEqual(response.status_code, 201)
                transaction.set_rollback(True)

        self.enqueue.assert_not_called()
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)

    def test_failed_payment_creation_does_not_enqueue_notification(self):
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal("0.10"))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(BORROWINGS_URL, self.payload)

        self.assertEqual(response.status_code, 400)
        self.enqueue.assert_not_called()
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)

    def test_unavailable_book_does_not_enqueue_notification(self):
        for inventory, is_active in ((0, True), (3, False)):
            with self.subTest(inventory=inventory, is_active=is_active):
                Book.objects.filter(pk=self.book.pk).update(
                    inventory=inventory, is_active=is_active
                )
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(BORROWINGS_URL, self.payload)

                self.assertEqual(response.status_code, 400)
                self.enqueue.assert_not_called()
                self.assertFalse(Borrowing.objects.exists())

    def test_unauthenticated_request_does_not_enqueue_notification(self):
        self.client.force_authenticate(user=None)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(BORROWINGS_URL, self.payload)

        self.assertEqual(response.status_code, 401)
        self.enqueue.assert_not_called()
        self.assertFalse(Borrowing.objects.exists())

    def test_reading_created_borrowing_does_not_repeat_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self.client.post(BORROWINGS_URL, self.payload)
        self.assertEqual(created.status_code, 201)
        self.enqueue.assert_called_once()
        self.enqueue.reset_mock()
        detail_url = reverse("borrowings:borrowings-detail", args=[created.data["id"]])

        with self.captureOnCommitCallbacks(execute=True):
            listing = self.client.get(BORROWINGS_URL)
            detail = self.client.get(detail_url)

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.enqueue.assert_not_called()
