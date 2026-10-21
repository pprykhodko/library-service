from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from books.models import Book
from borrowings.models import Borrowing
from notifications.tasks import notify_overdue_borrowings, send_telegram_notification
from payments.models import Payment
from users.models import User


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    TIME_ZONE="Europe/Vienna",
)
class OverdueBorrowingNotificationTests(TestCase):
    today = date(2026, 10, 21)
    user: User
    other_user: User
    book: Book

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user("reader@example.com")
        cls.other_user = User.objects.create_user("other@example.com")
        cls.book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=2,
            daily_fee=Decimal("1.50"),
        )

    def setUp(self) -> None:
        enqueue = patch.object(send_telegram_notification, "delay")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)

        clock = patch(
            "django.utils.timezone.now",
            return_value=datetime(2026, 10, 21, 10, tzinfo=UTC),
        )
        self.now = clock.start()
        self.addCleanup(clock.stop)

        network_guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("These tests must not contact Telegram"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def make_borrowing(
        self,
        *,
        due_date: date | None = None,
        returned: bool = False,
        user: User | None = None,
    ) -> Borrowing:
        return Borrowing.objects.create(
            book=self.book,
            user=user or self.user,
            borrow_date=self.today - timedelta(days=10),
            expected_return_date=due_date or self.today - timedelta(days=3),
            actual_return_date=self.today if returned else None,
            daily_fee=self.book.daily_fee,
        )

    def test_empty_database_requires_no_notifications(self):
        self.assertEqual(notify_overdue_borrowings.run(), 0)
        self.enqueue.assert_not_called()

    def test_only_unreturned_borrowings_due_before_today_are_selected(self):
        overdue = self.make_borrowing()
        self.make_borrowing(returned=True)
        self.make_borrowing(due_date=self.today)
        self.make_borrowing(due_date=self.today + timedelta(days=1))

        self.assertEqual(notify_overdue_borrowings.run(), 1)

        self.enqueue.assert_called_once()
        text = self.enqueue.call_args.args[0]
        self.assertIsInstance(text, str)
        for detail in (
            f"Borrowing #{overdue.pk}",
            self.book.title,
            self.user.email,
            overdue.expected_return_date.isoformat(),
            "Overdue days: 3",
        ):
            with self.subTest(detail=detail):
                self.assertIn(detail, text)

    def test_multiple_overdue_borrowings_for_one_reader_each_get_a_message(self):
        borrowings = [
            self.make_borrowing(),
            self.make_borrowing(due_date=self.today - timedelta(days=1)),
        ]

        self.assertEqual(notify_overdue_borrowings.run(), 2)

        self.assertEqual(self.enqueue.call_count, 2)
        texts = [call.args[0] for call in self.enqueue.call_args_list]
        for borrowing in borrowings:
            with self.subTest(borrowing_id=borrowing.pk):
                matching = [
                    text for text in texts if f"Borrowing #{borrowing.pk}" in text
                ]
                self.assertEqual(len(matching), 1)
                self.assertIn(self.user.email, matching[0])

    def test_messages_identify_the_correct_reader(self):
        borrowings = [
            self.make_borrowing(user=self.user),
            self.make_borrowing(user=self.other_user),
        ]

        self.assertEqual(notify_overdue_borrowings.run(), 2)

        texts = [call.args[0] for call in self.enqueue.call_args_list]
        self.assertEqual(len(texts), 2)
        for borrowing in borrowings:
            with self.subTest(borrowing_id=borrowing.pk):
                matching = [
                    text for text in texts if f"Borrowing #{borrowing.pk}" in text
                ]
                self.assertEqual(len(matching), 1)
                self.assertIn(borrowing.user.email, matching[0])

    def test_payment_status_and_inactive_book_do_not_hide_overdue_borrowings(self):
        Book.objects.filter(pk=self.book.pk).update(is_active=False)
        for status in Payment.Status.values:
            borrowing = self.make_borrowing()
            Payment.objects.create(
                borrowing=borrowing,
                money_to_pay=Decimal("4.50"),
                status=status,
            )

        self.assertEqual(notify_overdue_borrowings.run(), 3)
        self.assertEqual(self.enqueue.call_count, 3)

    def test_check_does_not_change_inventory_borrowings_or_create_fines(self):
        borrowing = self.make_borrowing()
        Payment.objects.create(
            borrowing=borrowing,
            money_to_pay=Decimal("4.50"),
            status=Payment.Status.PAID,
        )
        books_before = list(Book.objects.order_by("pk").values())
        borrowings_before = list(Borrowing.objects.order_by("pk").values())
        payments_before = list(Payment.objects.order_by("pk").values())

        self.assertEqual(notify_overdue_borrowings.run(), 1)

        self.assertEqual(list(Book.objects.order_by("pk").values()), books_before)
        self.assertEqual(
            list(Borrowing.objects.order_by("pk").values()), borrowings_before
        )
        self.assertEqual(list(Payment.objects.order_by("pk").values()), payments_before)

    def test_overdue_boundary_uses_local_date_instead_of_utc_date(self):
        self.now.return_value = datetime(2026, 10, 20, 22, 30, tzinfo=UTC)
        overdue = self.make_borrowing(due_date=date(2026, 10, 20))
        self.make_borrowing(due_date=date(2026, 10, 21))

        with timezone.override("Europe/Vienna"):
            self.assertEqual(notify_overdue_borrowings.run(), 1)

        self.enqueue.assert_called_once()
        text = self.enqueue.call_args.args[0]
        self.assertIn(f"Borrowing #{overdue.pk}", text)
        self.assertIn("Overdue days: 1", text)

    def test_returned_book_is_excluded_from_next_check(self):
        borrowing = self.make_borrowing()
        self.assertEqual(notify_overdue_borrowings.run(), 1)
        self.enqueue.assert_called_once()
        self.enqueue.reset_mock()
        Borrowing.objects.filter(pk=borrowing.pk).update(actual_return_date=self.today)

        self.assertEqual(notify_overdue_borrowings.run(), 0)
        self.enqueue.assert_not_called()
