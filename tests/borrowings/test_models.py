from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from books.models import Book
from borrowings.models import Borrowing
from users.models import User


class BorrowingModelTests(TestCase):
    book: Book
    user: User
    borrowing: Borrowing

    @classmethod
    def setUpTestData(cls) -> None:
        cls.book = Book.objects.create(
            title="The Hobbit", author="J. R. R. Tolkien",
            inventory=3, daily_fee=Decimal("1.50"),
        )
        cls.user = User.objects.create_user("reader@example.com")

    def setUp(self) -> None:
        self.borrow_date = date(2026, 10, 19)
        self.borrowing = Borrowing(
            book=self.book, user=self.user, borrow_date=self.borrow_date,
            expected_return_date=self.borrow_date + timedelta(days=3),
            daily_fee=Decimal("1.50"),
        )

    def test_valid_borrowing_can_be_saved(self):
        self.borrowing.full_clean()
        self.borrowing.save()
        self.borrowing.refresh_from_db()

        self.assertEqual(self.borrowing.book_id, self.book.pk)
        self.assertEqual(self.borrowing.user_id, self.user.pk)
        self.assertEqual(self.borrowing.borrow_date, self.borrow_date)
        self.assertEqual(self.borrowing.expected_return_date,
                         self.borrow_date + timedelta(days=3))
        self.assertIsNone(self.borrowing.actual_return_date)
        self.assertEqual(self.borrowing.daily_fee, Decimal("1.50"))

    def test_default_borrow_date_is_current_local_date(self):
        before = timezone.localdate()
        borrowing = Borrowing(
            book=self.book, user=self.user, expected_return_date=before,
        )
        after = timezone.localdate()
        self.assertIn(borrowing.borrow_date, (before, after))
        self.assertIsNone(borrowing.actual_return_date)

    def test_same_day_borrowing_and_return_are_allowed(self):
        self.borrowing.expected_return_date = self.borrow_date
        self.borrowing.actual_return_date = self.borrow_date
        self.borrowing.full_clean()
        self.borrowing.save()
        self.borrowing.refresh_from_db()

        self.assertEqual(self.borrowing.actual_return_date, self.borrow_date)

    def test_return_after_expected_date_is_allowed(self):
        late_date = self.borrowing.expected_return_date + timedelta(days=2)
        self.borrowing.actual_return_date = late_date
        self.borrowing.full_clean()
        self.borrowing.save()
        self.borrowing.refresh_from_db()

        self.assertEqual(self.borrowing.actual_return_date, late_date)

    def test_expected_return_before_borrow_date_is_invalid(self):
        self.borrowing.expected_return_date = self.borrow_date - timedelta(days=1)
        with self.assertRaises(ValidationError) as error:
            self.borrowing.full_clean()
        self.assertIn("expected_return_date", error.exception.message_dict)

    def test_actual_return_before_borrow_date_is_invalid(self):
        self.borrowing.actual_return_date = self.borrow_date - timedelta(days=1)
        with self.assertRaises(ValidationError) as error:
            self.borrowing.full_clean()
        self.assertIn("actual_return_date", error.exception.message_dict)

    def test_clean_reports_both_invalid_dates(self):
        yesterday = self.borrow_date - timedelta(days=1)
        self.borrowing.expected_return_date = yesterday
        self.borrowing.actual_return_date = yesterday
        with self.assertRaises(ValidationError) as error:
            self.borrowing.clean()
        self.assertSetEqual(set(error.exception.message_dict), {
            "expected_return_date", "actual_return_date",
        })

    def test_legacy_null_fee_and_zero_fee_are_allowed(self):
        for fee in (None, Decimal("0.00")):
            with self.subTest(fee=fee):
                self.borrowing.daily_fee = fee
                self.borrowing.full_clean()
                self.borrowing.save()
                self.borrowing.refresh_from_db()
                self.assertEqual(self.borrowing.daily_fee, fee)

    def test_negative_fee_is_invalid(self):
        self.borrowing.daily_fee = Decimal("-0.01")
        with self.assertRaises(ValidationError) as error:
            self.borrowing.full_clean()
        self.assertIn("daily_fee", error.exception.message_dict)

    def test_database_rejects_invalid_dates_and_fee_without_validation(self):
        self.borrowing.save()
        yesterday = self.borrow_date - timedelta(days=1)
        for changes in (
            {"expected_return_date": yesterday},
            {"actual_return_date": yesterday},
            {"daily_fee": Decimal("-0.01")},
        ):
            with self.subTest(changes=changes):
                # update() bypasses full_clean(); DB constraints still protect the row.
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Borrowing.objects.filter(pk=self.borrowing.pk).update(**changes)
                self.borrowing.refresh_from_db()
                self.assertEqual(self.borrowing.expected_return_date,
                                 self.borrow_date + timedelta(days=3))
                self.assertIsNone(self.borrowing.actual_return_date)
                self.assertEqual(self.borrowing.daily_fee, Decimal("1.50"))

    def test_saved_fee_does_not_follow_book_price_changes(self):
        self.borrowing.save()
        self.book.daily_fee = Decimal("9.00")
        self.book.save(update_fields=["daily_fee"])
        self.borrowing.refresh_from_db()

        self.assertEqual(self.borrowing.daily_fee, Decimal("1.50"))

    def test_active_and_returned_borrowings_protect_book_and_user(self):
        self.borrowing.save()
        for return_date in (None, self.borrow_date):
            self.borrowing.actual_return_date = return_date
            self.borrowing.save(update_fields=["actual_return_date"])
            for related in (self.book, self.user):
                with self.subTest(
                    return_date=return_date, model=type(related).__name__,
                ):
                    with self.assertRaises(ProtectedError):
                        related.delete()
                    self.assertTrue(Book.objects.filter(pk=self.book.pk).exists())
                    self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
                    self.assertTrue(
                        Borrowing.objects.filter(pk=self.borrowing.pk).exists(),
                    )

    def test_default_ordering_uses_date_then_descending_id(self):
        self.borrowing.save()
        older = Borrowing.objects.create(
            book=self.book, user=self.user,
            borrow_date=self.borrow_date - timedelta(days=1),
            expected_return_date=self.borrow_date,
        )
        same_day = Borrowing.objects.create(
            book=self.book, user=self.user, borrow_date=self.borrow_date,
            expected_return_date=self.borrow_date,
        )

        self.assertEqual(list(Borrowing.objects.values_list("pk", flat=True)),
                         [same_day.pk, self.borrowing.pk, older.pk])

    def test_string_representation_identifies_borrowing_book_and_user(self):
        self.borrowing.save()
        self.assertEqual(
            str(self.borrowing),
            f"Borrowing {self.borrowing.pk}: book {self.book.pk}, user {self.user.pk}",
        )
