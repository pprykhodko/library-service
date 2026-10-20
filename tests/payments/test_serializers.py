from decimal import Decimal

from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from books.models import Book
from borrowings.models import Borrowing
from payments.models import Payment
from payments.serializers import (
    PaymentResultSerializer,
    PaymentSerializer,
    SessionQuerySerializer,
)
from users.models import User


class PaymentSerializerTests(TestCase):
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
        borrowing = Borrowing.objects.create(
            book=book,
            user=user,
            expected_return_date=timezone.localdate(),
            daily_fee=book.daily_fee,
        )
        cls.payment = Payment.objects.create(
            borrowing=borrowing,
            money_to_pay=Decimal("4.50"),
        )

    def test_representation_contains_public_fields_and_formatted_amount(self):
        data = PaymentSerializer(self.payment).data

        self.assertEqual(
            data,
            {
                "id": self.payment.pk,
                "borrowing": self.payment.borrowing_id,
                "status": "PENDING",
                "type": "PAYMENT",
                "session_url": "",
                "session_id": None,
                "money_to_pay": "4.50",
            },
        )
        self.assertNotIn("checkout_key", data)

    def test_existing_checkout_session_is_included_in_response(self):
        self.payment.session_id = "cs_test_example"
        self.payment.session_url = "https://checkout.stripe.com/c/pay/cs_test_example"
        self.payment.save(update_fields=["session_id", "session_url"])
        self.payment.refresh_from_db()
        data = PaymentSerializer(self.payment).data

        self.assertEqual(data["session_id"], "cs_test_example")
        self.assertEqual(data["session_url"], self.payment.session_url)

    def test_zero_amount_and_fine_statuses_are_serialized(self):
        self.payment.type = Payment.Type.FINE
        self.payment.money_to_pay = Decimal("0.00")
        for status in (Payment.Status.PAID, Payment.Status.EXPIRED):
            with self.subTest(status=status):
                self.payment.status = status
                data = PaymentSerializer(self.payment).data
                self.assertEqual(data["type"], "FINE")
                self.assertEqual(data["status"], status)
                self.assertEqual(data["money_to_pay"], "0.00")

    def test_input_cannot_change_read_only_payment_fields(self):
        original_id = self.payment.pk
        original_key = self.payment.checkout_key
        original_data = dict(PaymentSerializer(self.payment).data)
        for partial in (False, True):
            with self.subTest(partial=partial):
                serializer = PaymentSerializer(
                    self.payment,
                    data={
                        "id": 999999,
                        "borrowing": 999999,
                        "status": "PAID",
                        "type": "FINE",
                        "session_id": "cs_test_forged",
                        "session_url": "https://example.com/forged",
                        "money_to_pay": "0.00",
                        "checkout_key": "00000000-0000-0000-0000-000000000000",
                    },
                    partial=partial,
                )

                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(serializer.validated_data, {})
                serializer.save()
                self.payment.refresh_from_db()
                self.assertEqual(self.payment.pk, original_id)
                self.assertEqual(self.payment.checkout_key, original_key)
                self.assertEqual(PaymentSerializer(self.payment).data, original_data)
                self.assertEqual(Payment.objects.count(), 1)


class SessionQuerySerializerTests(SimpleTestCase):
    def test_session_id_is_read_from_query_parameters_and_trimmed(self):
        query = QueryDict("session_id=%20cs_test_example%20")
        serializer = SessionQuerySerializer(data=query.dict())

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {"session_id": "cs_test_example"})

    def test_missing_empty_or_null_session_id_is_rejected(self):
        cases: tuple[dict[str, object], ...] = (
            {},
            {"session_id": ""},
            {"session_id": " \t\n "},
            {"session_id": None},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                serializer = SessionQuerySerializer(data=payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn("session_id", serializer.errors)

    def test_session_id_length_limit(self):
        serializer = SessionQuerySerializer(data={"session_id": "s" * 255})
        self.assertTrue(serializer.is_valid(), serializer.errors)

        serializer = SessionQuerySerializer(data={"session_id": "s" * 256})
        self.assertFalse(serializer.is_valid())
        self.assertIn("session_id", serializer.errors)

    def test_structured_values_are_not_accepted_as_session_id(self):
        for value in (["cs_test_example"], {"id": "cs_test_example"}, True):
            with self.subTest(value=value):
                serializer = SessionQuerySerializer(data={"session_id": value})
                self.assertFalse(serializer.is_valid())
                self.assertIn("session_id", serializer.errors)


class PaymentResultSerializerTests(SimpleTestCase):
    def test_success_and_cancel_response_format(self):
        for status, detail in (
            ("PAID", "Payment confirmed."),
            ("PENDING", "Payment has not been confirmed by Stripe yet"),
            ("cancelled", "Checkout paused."),
        ):
            with self.subTest(status=status):
                serializer = PaymentResultSerializer(
                    {
                        "status": status,
                        "detail": detail,
                        "internal_field": "private",
                    }
                )
                self.assertEqual(serializer.data, {"status": status, "detail": detail})
