from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from books.models import Book
from borrowings.models import Borrowing
from borrowings.serializers import (
    BorrowingDetailSerializer,
    BorrowingFilterSerializer,
    BorrowingListSerializer,
    BorrowingSerializer,
)
from payments.models import Payment
from users.models import User


class BorrowingSerializerTests(TestCase):
    book: Book
    user: User
    other_user: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=3,
            daily_fee=Decimal("1.50"),
        )
        cls.user = User.objects.create_user("reader@example.com")
        cls.other_user = User.objects.create_user("other@example.com")

    def setUp(self) -> None:
        self.today = timezone.localdate()
        self.payload: dict[str, object] = {
            "book": self.book.pk,
            "expected_return_date": (self.today + timedelta(days=3)).isoformat(),
        }

    def valid_serializer(self) -> BorrowingSerializer:
        serializer = BorrowingSerializer(data=self.payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer

    def assert_no_creation(self) -> None:
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_validation_converts_book_id_and_date_without_writing_to_database(self):
        serializer = self.valid_serializer()
        self.assertEqual(serializer.validated_data["book"], self.book)
        self.assertEqual(
            serializer.validated_data["expected_return_date"],
            self.today + timedelta(days=3),
        )
        self.assert_no_creation()

    def test_required_fields_cannot_be_omitted(self):
        for field in ("book", "expected_return_date"):
            with self.subTest(field=field):
                payload = self.payload.copy()
                del payload[field]
                serializer = BorrowingSerializer(data=payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn(field, serializer.errors)
        self.assert_no_creation()

    def test_invalid_dates_are_rejected(self):
        for value in (
            (self.today - timedelta(days=1)).isoformat(),
            "not-a-date",
            "2026-02-30",
            None,
        ):
            with self.subTest(value=value):
                self.payload["expected_return_date"] = value
                serializer = BorrowingSerializer(data=self.payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn("expected_return_date", serializer.errors)
        self.assert_no_creation()

    def test_invalid_book_ids_are_rejected(self):
        for value in (999999, "not-an-id", None):
            with self.subTest(value=value):
                self.payload["book"] = value
                serializer = BorrowingSerializer(data=self.payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn("book", serializer.errors)
        self.assert_no_creation()

    def test_inactive_and_sold_out_books_are_rejected(self):
        for active, inventory in ((False, 3), (True, 0)):
            with self.subTest(active=active, inventory=inventory):
                Book.objects.filter(pk=self.book.pk).update(
                    is_active=active,
                    inventory=inventory,
                )
                serializer = BorrowingSerializer(data=self.payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn("book", serializer.errors)
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_save_creates_borrowing_and_rental_payment_and_decrements_inventory(self):
        borrowing = self.valid_serializer().save(user=self.user)
        borrowing.refresh_from_db()
        self.book.refresh_from_db()
        payment = borrowing.payments.get()

        self.assertEqual(borrowing.user_id, self.user.pk)
        self.assertEqual(borrowing.book_id, self.book.pk)
        self.assertEqual(borrowing.borrow_date, self.today)
        self.assertEqual(borrowing.daily_fee, Decimal("1.50"))
        self.assertIsNone(borrowing.actual_return_date)
        self.assertEqual(self.book.inventory, 2)
        self.assertEqual(payment.type, Payment.Type.PAYMENT)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.money_to_pay, Decimal("4.50"))
        self.assertIsNone(payment.session_id)
        self.assertEqual(payment.session_url, "")

    def test_same_day_rental_is_charged_for_one_day(self):
        self.payload["expected_return_date"] = self.today.isoformat()
        borrowing = self.valid_serializer().save(user=self.user)
        self.assertEqual(borrowing.payments.get().money_to_pay, Decimal("1.50"))

    def test_client_cannot_override_server_controlled_fields(self):
        self.payload.update(
            {
                "id": 999999,
                "user": self.other_user.pk,
                "borrow_date": "2000-01-01",
                "actual_return_date": self.today.isoformat(),
                "daily_fee": "0.00",
                "payments": [{"status": "PAID"}],
            }
        )
        serializer = self.valid_serializer()
        self.assertSetEqual(
            set(serializer.validated_data), {"book", "expected_return_date"}
        )
        borrowing = serializer.save(user=self.user)

        self.assertNotEqual(borrowing.pk, 999999)
        self.assertEqual(borrowing.user_id, self.user.pk)
        self.assertEqual(borrowing.borrow_date, self.today)
        self.assertIsNone(borrowing.actual_return_date)
        self.assertEqual(borrowing.daily_fee, Decimal("1.50"))
        self.assertEqual(borrowing.payments.get().status, Payment.Status.PENDING)

    def test_save_uses_current_book_price_after_validation(self):
        serializer = self.valid_serializer()
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal("2.00"))
        borrowing = serializer.save(user=self.user)

        self.assertEqual(borrowing.daily_fee, Decimal("2.00"))
        self.assertEqual(borrowing.payments.get().money_to_pay, Decimal("6.00"))

    def test_save_rechecks_availability_after_validation(self):
        for active, inventory in ((False, 3), (True, 0)):
            with self.subTest(active=active, inventory=inventory):
                Book.objects.filter(pk=self.book.pk).update(is_active=True, inventory=3)
                serializer = self.valid_serializer()
                Book.objects.filter(pk=self.book.pk).update(
                    is_active=active,
                    inventory=inventory,
                )
                with self.assertRaises(ValidationError) as error:
                    serializer.save(user=self.user)
                self.assertIn("book", error.exception.detail)
                self.book.refresh_from_db()
                self.assertEqual(self.book.inventory, inventory)
                self.assertFalse(Borrowing.objects.exists())
                self.assertFalse(Payment.objects.exists())

    def test_save_rechecks_date_if_day_changes_after_validation(self):
        self.payload["expected_return_date"] = self.today.isoformat()
        serializer = self.valid_serializer()
        with patch(
            "borrowings.serializers.timezone.localdate",
            return_value=self.today + timedelta(days=1),
        ):
            with self.assertRaises(ValidationError) as error:
                serializer.save(user=self.user)
        self.assertIn("expected_return_date", error.exception.detail)
        self.assert_no_creation()

    def test_pending_and_expired_rentals_and_fines_block_creation(self):
        existing = Borrowing.objects.create(
            book=self.book,
            user=self.user,
            expected_return_date=self.today,
        )
        for status in (Payment.Status.PENDING, Payment.Status.EXPIRED):
            for payment_type in (Payment.Type.PAYMENT, Payment.Type.FINE):
                with self.subTest(status=status, payment_type=payment_type):
                    payment = Payment.objects.create(
                        borrowing=existing,
                        status=status,
                        type=payment_type,
                        money_to_pay=Decimal("1.50"),
                    )
                    serializer = self.valid_serializer()
                    with self.assertRaises(ValidationError) as error:
                        serializer.save(user=self.user)
                    self.assertIn("detail", error.exception.detail)
                    self.book.refresh_from_db()
                    self.assertEqual(self.book.inventory, 3)
                    self.assertEqual(Borrowing.objects.count(), 1)
                    self.assertEqual(Payment.objects.count(), 1)
                    payment.delete()

    def test_paid_payments_do_not_block_new_borrowing(self):
        existing = Borrowing.objects.create(
            book=self.book,
            user=self.user,
            expected_return_date=self.today,
        )
        for payment_type in (Payment.Type.PAYMENT, Payment.Type.FINE):
            Payment.objects.create(
                borrowing=existing,
                status=Payment.Status.PAID,
                type=payment_type,
                money_to_pay=Decimal("1.50"),
            )
        borrowing = self.valid_serializer().save(user=self.user)
        self.assertNotEqual(borrowing.pk, existing.pk)
        self.assertEqual(Borrowing.objects.count(), 2)
        self.assertEqual(borrowing.payments.get().status, Payment.Status.PENDING)

    def test_another_users_debt_does_not_block_creation(self):
        existing = Borrowing.objects.create(
            book=self.book,
            user=self.other_user,
            expected_return_date=self.today,
        )
        Payment.objects.create(borrowing=existing, money_to_pay=Decimal("1.50"))
        borrowing = self.valid_serializer().save(user=self.user)
        self.assertEqual(borrowing.user_id, self.user.pk)
        self.assertEqual(Borrowing.objects.count(), 2)

    def test_payment_amount_error_rolls_back_borrowing_and_inventory(self):
        for fee in (Decimal("0.10"), Decimal("1000000.00")):
            with self.subTest(fee=fee):
                Book.objects.filter(pk=self.book.pk).update(daily_fee=fee)
                self.payload["expected_return_date"] = self.today.isoformat()
                serializer = self.valid_serializer()
                with self.assertRaises(ValidationError) as error:
                    serializer.save(user=self.user)
                self.assertIn("expected_return_date", error.exception.detail)
                self.assert_no_creation()

    def test_free_rental_creates_paid_zero_amount_payment(self):
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal("0.00"))
        borrowing = self.valid_serializer().save(user=self.user)
        payment = borrowing.payments.get()
        self.assertEqual(payment.money_to_pay, Decimal("0.00"))
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 2)

    def test_list_representation_uses_title_email_and_nested_payments(self):
        borrowing = self.valid_serializer().save(user=self.user)
        data = BorrowingListSerializer(borrowing).data

        self.assertEqual(data["book"], self.book.title)
        self.assertEqual(data["user"], self.user.email)
        self.assertEqual(data["daily_fee"], "1.50")
        self.assertEqual(
            data["expected_return_date"],
            self.payload["expected_return_date"],
        )
        self.assertEqual(len(data["payments"]), 1)
        self.assertEqual(data["payments"][0]["id"], borrowing.payments.get().pk)

    def test_detail_representation_includes_book_and_public_user_fields(self):
        borrowing = self.valid_serializer().save(user=self.user)
        data = BorrowingDetailSerializer(borrowing).data

        self.assertEqual(data["book"]["id"], self.book.pk)
        self.assertEqual(data["book"]["title"], self.book.title)
        self.assertEqual(data["user"]["id"], self.user.pk)
        self.assertSetEqual(
            set(data["user"]),
            {"id", "email", "first_name", "last_name"},
        )
        self.assertEqual(data["payments"][0]["money_to_pay"], "4.50")


class BorrowingFilterSerializerTests(SimpleTestCase):
    def test_empty_query_does_not_add_is_active_filter(self):
        serializer = BorrowingFilterSerializer(data=QueryDict("").dict())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {})

    def test_user_id_is_converted_without_adding_boolean_filter(self):
        serializer = BorrowingFilterSerializer(data=QueryDict("user_id=42").dict())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {"user_id": 42})

    def test_invalid_user_ids_are_rejected(self):
        for value in ("0", "-1", "1.5", "abc", "", None):
            with self.subTest(value=value):
                serializer = BorrowingFilterSerializer(data={"user_id": value})
                self.assertFalse(serializer.is_valid())
                self.assertIn("user_id", serializer.errors)

    def test_boolean_strings_are_converted(self):
        cases: tuple[tuple[str, bool], ...] = (
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                query = QueryDict(f"user_id=42&is_active={value}")
                serializer = BorrowingFilterSerializer(data=query.dict())
                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(serializer.validated_data["user_id"], 42)
                self.assertIs(serializer.validated_data["is_active"], expected)

    def test_invalid_boolean_values_are_rejected(self):
        for value in ("maybe", "2", "", None):
            with self.subTest(value=value):
                serializer = BorrowingFilterSerializer(data={"is_active": value})
                self.assertFalse(serializer.is_valid())
                self.assertIn("is_active", serializer.errors)
