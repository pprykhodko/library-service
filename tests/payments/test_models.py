import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from books.models import Book
from borrowings.models import Borrowing
from payments.models import ExpiredCheckoutSession, Payment
from users.models import User


class PaymentModelTests(TestCase):
    borrowing: Borrowing
    other_borrowing: Borrowing
    payment: Payment

    @classmethod
    def setUpTestData(cls) -> None:
        book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=3,
            daily_fee=Decimal("1.50"),
        )
        user = User.objects.create_user("reader@example.com")
        cls.borrowing = Borrowing.objects.create(
            book=book,
            user=user,
            expected_return_date=timezone.localdate(),
            daily_fee=book.daily_fee,
        )
        cls.other_borrowing = Borrowing.objects.create(
            book=book,
            user=user,
            expected_return_date=timezone.localdate(),
            daily_fee=book.daily_fee,
        )

    def setUp(self) -> None:
        self.payment = Payment(
            borrowing=self.borrowing,
            money_to_pay=Decimal("4.50"),
        )

    def test_valid_payment_can_be_saved_with_defaults(self):
        self.payment.full_clean()
        self.payment.save()
        self.payment.refresh_from_db()

        self.assertEqual(self.payment.borrowing_id, self.borrowing.pk)
        self.assertEqual(self.payment.money_to_pay, Decimal("4.50"))
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertEqual(self.payment.type, Payment.Type.PAYMENT)
        self.assertIsNone(self.payment.session_id)
        self.assertEqual(self.payment.session_url, "")
        self.assertIsInstance(self.payment.checkout_key, uuid.UUID)

    def test_zero_amount_is_allowed(self):
        self.payment.money_to_pay = Decimal("0.00")
        self.payment.full_clean()
        self.payment.save()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.money_to_pay, Decimal("0.00"))

    def test_negative_amount_is_invalid(self):
        self.payment.money_to_pay = Decimal("-0.01")
        with self.assertRaises(ValidationError) as error:
            self.payment.full_clean()
        self.assertIn("money_to_pay", error.exception.message_dict)

    def test_amount_precision_and_model_size_limits(self):
        for amount in (Decimal("1.001"), Decimal("10000000000.00")):
            with self.subTest(amount=amount):
                self.payment.money_to_pay = amount
                with self.assertRaises(ValidationError) as error:
                    self.payment.full_clean()
                self.assertIn("money_to_pay", error.exception.message_dict)

    def test_supported_statuses_and_types_are_valid(self):
        for status in (
            Payment.Status.PENDING,
            Payment.Status.PAID,
            Payment.Status.EXPIRED,
        ):
            for payment_type in (Payment.Type.PAYMENT, Payment.Type.FINE):
                with self.subTest(status=status, payment_type=payment_type):
                    self.payment.status = status
                    self.payment.type = payment_type
                    self.payment.full_clean()
                    self.payment.save()
                    self.payment.refresh_from_db()
                    self.assertEqual(self.payment.status, status)
                    self.assertEqual(self.payment.type, payment_type)

    def test_invalid_status_and_type_are_rejected_by_validation(self):
        for field, value in (("status", "UNKNOWN"), ("type", "OTHER")):
            with self.subTest(field=field):
                original = getattr(self.payment, field)
                setattr(self.payment, field, value)
                with self.assertRaises(ValidationError) as error:
                    self.payment.full_clean()
                self.assertIn(field, error.exception.message_dict)
                setattr(self.payment, field, original)

    def test_database_rejects_invalid_amount_status_and_type(self):
        self.payment.save()
        for changes in (
            {"money_to_pay": Decimal("-0.01")},
            {"status": "UNKNOWN"},
            {"type": "OTHER"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Payment.objects.filter(pk=self.payment.pk).update(**changes)
                self.payment.refresh_from_db()
                self.assertEqual(self.payment.money_to_pay, Decimal("4.50"))
                self.assertEqual(self.payment.status, Payment.Status.PENDING)
                self.assertEqual(self.payment.type, Payment.Type.PAYMENT)

    def test_rental_and_fine_can_coexist_for_one_borrowing(self):
        self.payment.save()
        fine = Payment.objects.create(
            borrowing=self.borrowing,
            type=Payment.Type.FINE,
            money_to_pay=Decimal("3.00"),
        )
        self.assertSetEqual(
            set(self.borrowing.payments.values_list("pk", flat=True)),
            {self.payment.pk, fine.pk},
        )

    def test_duplicate_payment_type_is_rejected_even_if_original_is_paid(self):
        for payment_type in (Payment.Type.PAYMENT, Payment.Type.FINE):
            with self.subTest(payment_type=payment_type):
                original = Payment.objects.create(
                    borrowing=self.borrowing,
                    type=payment_type,
                    status=Payment.Status.PAID,
                    money_to_pay=Decimal("1.50"),
                )
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Payment.objects.create(
                        borrowing=self.borrowing,
                        type=payment_type,
                        money_to_pay=Decimal("1.50"),
                    )
                self.assertEqual(
                    self.borrowing.payments.filter(type=payment_type).count(),
                    1,
                )
                self.assertTrue(Payment.objects.filter(pk=original.pk).exists())

    def test_same_payment_type_is_allowed_for_different_borrowings(self):
        self.payment.save()
        other = Payment.objects.create(
            borrowing=self.other_borrowing,
            money_to_pay=Decimal("1.50"),
        )
        self.assertEqual(Payment.objects.count(), 2)
        self.assertNotEqual(self.payment.checkout_key, other.checkout_key)
        self.assertIsNone(self.payment.session_id)
        self.assertIsNone(other.session_id)

    def test_session_id_and_checkout_key_must_be_unique(self):
        self.payment.session_id = "cs_test_unique"
        self.payment.save()
        other = Payment.objects.create(
            borrowing=self.other_borrowing,
            money_to_pay=Decimal("1.50"),
        )
        original_key = other.checkout_key
        for changes in (
            {"session_id": self.payment.session_id},
            {"checkout_key": self.payment.checkout_key},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Payment.objects.filter(pk=other.pk).update(**changes)
                other.refresh_from_db()
                self.assertIsNone(other.session_id)
                self.assertEqual(other.checkout_key, original_key)

    def test_invalid_session_url_is_rejected(self):
        self.payment.session_url = "not-a-url"
        with self.assertRaises(ValidationError) as error:
            self.payment.full_clean()
        self.assertIn("session_url", error.exception.message_dict)

    def test_payment_protects_borrowing_from_deletion(self):
        self.payment.save()
        with self.assertRaises(ProtectedError):
            self.borrowing.delete()
        self.assertTrue(Borrowing.objects.filter(pk=self.borrowing.pk).exists())
        self.assertTrue(Payment.objects.filter(pk=self.payment.pk).exists())

    def test_default_ordering_is_newest_first(self):
        self.payment.save()
        fine = Payment.objects.create(
            borrowing=self.borrowing,
            type=Payment.Type.FINE,
            money_to_pay=Decimal("3.00"),
        )
        self.assertEqual(
            list(Payment.objects.values_list("pk", flat=True)),
            [fine.pk, self.payment.pk],
        )

    def test_string_representation_contains_amount_and_status(self):
        self.payment.save()
        self.assertEqual(
            str(self.payment),
            f"Payment {self.payment.pk}: 4.50 USD (PENDING)",
        )


class ExpiredCheckoutSessionModelTests(TestCase):
    payment: Payment
    other_payment: Payment

    @classmethod
    def setUpTestData(cls) -> None:
        book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=3,
            daily_fee=Decimal("1.50"),
        )
        user = User.objects.create_user("reader@example.com")
        borrowing = Borrowing.objects.create(
            book=book,
            user=user,
            expected_return_date=timezone.localdate(),
        )
        cls.payment = Payment.objects.create(
            borrowing=borrowing,
            money_to_pay=Decimal("4.50"),
        )
        cls.other_payment = Payment.objects.create(
            borrowing=borrowing,
            type=Payment.Type.FINE,
            money_to_pay=Decimal("3.00"),
        )

    def test_multiple_old_sessions_can_belong_to_one_payment(self):
        first = ExpiredCheckoutSession.objects.create(
            payment=self.payment,
            session_id="cs_test_old_1",
            checkout_key=uuid.uuid4(),
        )
        second = ExpiredCheckoutSession.objects.create(
            payment=self.payment,
            session_id="cs_test_old_2",
            checkout_key=uuid.uuid4(),
        )
        self.assertSetEqual(
            set(self.payment.expired_sessions.values_list("pk", flat=True)),
            {first.pk, second.pk},
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    def test_archived_session_id_and_key_are_unique_across_payments(self):
        session = ExpiredCheckoutSession.objects.create(
            payment=self.payment,
            session_id="cs_test_old",
            checkout_key=uuid.uuid4(),
        )
        for session_id, checkout_key in (
            (session.session_id, uuid.uuid4()),
            ("cs_test_other", session.checkout_key),
        ):
            with self.subTest(session_id=session_id):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ExpiredCheckoutSession.objects.create(
                        payment=self.other_payment,
                        session_id=session_id,
                        checkout_key=checkout_key,
                    )
        self.assertEqual(ExpiredCheckoutSession.objects.count(), 1)
        self.assertFalse(self.other_payment.expired_sessions.exists())

    def test_archived_session_protects_payment_from_deletion(self):
        session = ExpiredCheckoutSession.objects.create(
            payment=self.payment,
            session_id="cs_test_old",
            checkout_key=uuid.uuid4(),
        )
        with self.assertRaises(ProtectedError):
            self.payment.delete()
        self.assertTrue(Payment.objects.filter(pk=self.payment.pk).exists())
        self.assertTrue(ExpiredCheckoutSession.objects.filter(pk=session.pk).exists())
