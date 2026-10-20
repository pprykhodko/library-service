import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import patch

import stripe
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from books.models import Book
from borrowings.models import Borrowing
from payments.models import Payment
from users.models import User
from users.roles import EMPLOYEE, assign_role


PAYMENTS_URL = reverse("payments:payments-list")
WEBHOOK_URL = reverse("payments:webhook")
SUCCESS_URL = reverse("payments:success")
SECRET = "whsec_local_tests_only"


def payment_url(payment: Payment, action: str = "detail") -> str:
    return reverse(f"payments:payments-{action}", args=[payment.pk])


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET=SECRET,
    PAYMENTS_BASE_URL="http://testserver",
)
class PaymentApiTests(APITestCase):
    client: APIClient
    customer: User
    other_customer: User
    employee: User
    admin: User
    payment: Payment
    other_payment: Payment
    book: Book

    @classmethod
    def setUpTestData(cls) -> None:
        cls.customer = User.objects.create_user("reader@example.com")
        cls.other_customer = User.objects.create_user("other@example.com")
        cls.employee = User.objects.create_user("employee@example.com")
        assign_role(cls.employee, EMPLOYEE)
        cls.admin = User.objects.create_superuser("admin@example.com", "Password!2026")
        cls.book = Book.objects.create(
            title="The Hobbit", author="J. R. R. Tolkien",
            inventory=3, daily_fee=Decimal("1.50"),
        )
        for user, attribute in (
            (cls.customer, "payment"), (cls.other_customer, "other_payment"),
        ):
            borrowing = Borrowing.objects.create(
                book=cls.book, user=user, expected_return_date=timezone.localdate(),
                daily_fee=cls.book.daily_fee,
            )
            setattr(cls, attribute, Payment.objects.create(
                borrowing=borrowing, money_to_pay=Decimal("1.50"),
            ))

    def setUp(self) -> None:
        patcher = patch("payments.services.stripe.StripeClient")
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.sessions = self.factory.return_value.v1.checkout.sessions
        self.sessions.create.side_effect = self.create_session
        self.sessions.retrieve.side_effect = AssertionError("Unexpected Stripe lookup")

    def session_data(self, **overrides) -> dict:
        self.payment.refresh_from_db()
        data = {
            "object": "checkout.session", "id": "cs_test_current",
            "url": "https://checkout.stripe.com/c/pay/cs_test_current",
            "metadata": {"payment_id": str(self.payment.pk)},
            "client_reference_id": str(self.payment.checkout_key),
            "amount_total": 150, "currency": "usd", "mode": "payment",
            "status": "complete", "payment_status": "paid",
        }
        data.update(overrides)
        return data

    def create_session(self, *, params, options):
        return stripe.checkout.Session.construct_from(self.session_data(
            status="open", payment_status="unpaid",
            client_reference_id=params["client_reference_id"],
        ), "sk_test_fake")

    def mock_retrieve(self, data: dict) -> None:
        self.sessions.retrieve.side_effect = None
        self.sessions.retrieve.return_value = stripe.checkout.Session.construct_from(
            data, "sk_test_fake",
        )

    def signed_webhook(self, event_type: str, session: dict):
        payload = json.dumps({
            "id": "evt_test_example", "object": "event", "type": event_type,
            "data": {"object": session},
        }).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            SECRET.encode(), timestamp.encode() + b"." + payload, hashlib.sha256,
        ).hexdigest()
        return self.client.post(
            WEBHOOK_URL, payload, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=f"t={timestamp},v1={signature}",
        )

    def test_anonymous_cannot_list_read_or_start_checkout(self):
        self.assertEqual(self.client.get(PAYMENTS_URL).status_code, 401)
        self.assertEqual(self.client.get(payment_url(self.payment)).status_code, 401)
        for action in ("checkout", "renew"):
            with self.subTest(action=action):
                response = self.client.post(payment_url(self.payment, action))
                self.assertEqual(response.status_code, 401)
        self.factory.assert_not_called()

    def test_customer_sees_only_own_payments(self):
        self.client.force_authenticate(self.customer)
        response = self.client.get(PAYMENTS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], self.payment.pk)
        self.assertEqual(self.client.get(payment_url(self.payment)).status_code, 200)
        response = self.client.get(payment_url(self.other_payment))
        self.assertEqual(response.status_code, 404)

    def test_customer_cannot_checkout_or_renew_another_users_payment(self):
        self.client.force_authenticate(self.other_customer)
        for action in ("checkout", "renew"):
            with self.subTest(action=action):
                response = self.client.post(payment_url(self.payment, action))
                self.assertEqual(response.status_code, 404)
        self.factory.assert_not_called()

    def test_employee_and_admin_can_read_all_payments(self):
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                response = self.client.get(PAYMENTS_URL)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["count"], 2)
                self.assertEqual(self.client.get(payment_url(self.payment)).status_code,
                                 200)

    def test_employee_cannot_checkout_or_renew(self):
        self.client.force_authenticate(self.employee)
        for action in ("checkout", "renew"):
            with self.subTest(action=action):
                response = self.client.post(payment_url(self.payment, action))
                self.assertEqual(response.status_code, 403)
        self.factory.assert_not_called()

    def test_user_without_role_cannot_read_payments(self):
        self.customer.groups.clear()
        self.client.force_authenticate(self.customer)
        response = self.client.get(PAYMENTS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])
        self.assertEqual(self.client.get(payment_url(self.payment)).status_code, 404)

    def test_owner_and_admin_can_checkout_without_overriding_amount(self):
        for user in (self.customer, self.admin):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                self.mock_retrieve(self.session_data(
                    status="open", payment_status="unpaid",
                ))
                response = self.client.post(payment_url(self.payment, "checkout"), {
                    "money_to_pay": "0.00", "status": "PAID",
                }, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["id"], self.payment.pk)
                self.assertEqual(response.data["money_to_pay"], "1.50")
                self.assertEqual(response.data["status"], "PENDING")
                self.assertEqual(response.data["session_id"], "cs_test_current")
        self.sessions.create.assert_called_once()
        self.assertEqual(Payment.objects.count(), 2)

    def test_owner_and_admin_can_renew_expired_session(self):
        for user in (self.customer, self.admin):
            with self.subTest(user=user.email):
                old_id = f"cs_test_expired_{user.pk}"
                self.payment.session_id = old_id
                self.payment.status = Payment.Status.EXPIRED
                self.payment.save(update_fields=["session_id", "status"])
                self.mock_retrieve(self.session_data(
                    id=old_id, status="expired", payment_status="unpaid",
                ))
                self.client.force_authenticate(user)
                response = self.client.post(payment_url(self.payment, "renew"))
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["status"], "PENDING")
                self.assertEqual(response.data["session_id"], "cs_test_current")
                self.assertTrue(self.payment.expired_sessions.filter(
                    session_id=old_id,
                ).exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)
        self.assertEqual(Payment.objects.count(), 2)

    def test_provider_failure_returns_503_without_changing_payment(self):
        self.sessions.create.side_effect = stripe.APIConnectionError("Unavailable")
        self.client.force_authenticate(self.customer)
        response = self.client.post(payment_url(self.payment, "checkout"))
        self.assertEqual(response.status_code, 503)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertIsNone(self.payment.session_id)

    def test_open_session_cannot_be_renewed_and_expired_checkout_returns_400(self):
        self.payment.session_id = "cs_test_current"
        self.payment.save(update_fields=["session_id"])
        self.client.force_authenticate(self.customer)
        self.mock_retrieve(self.session_data(status="open", payment_status="unpaid"))
        response = self.client.post(payment_url(self.payment, "renew"))
        self.assertEqual(response.status_code, 400)
        self.mock_retrieve(self.session_data(status="expired", payment_status="unpaid"))
        response = self.client.post(payment_url(self.payment, "checkout"))
        self.assertEqual(response.status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.EXPIRED)
        self.sessions.create.assert_not_called()

    def test_direct_create_update_and_delete_are_unavailable_even_to_admin(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(PAYMENTS_URL, {}).status_code, 405)
        for method in (self.client.put, self.client.patch, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(payment_url(self.payment), {"status": "PAID"})
                self.assertEqual(response.status_code, 405)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    def test_pagination_orders_payments_newest_first(self):
        self.client.force_authenticate(self.admin)
        first = self.client.get(PAYMENTS_URL, {"page_size": 1})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], 2)
        self.assertEqual(first.data["results"][0]["id"], self.other_payment.pk)
        self.assertIsNotNone(first.data["next"])
        second = self.client.get(first.data["next"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["results"][0]["id"], self.payment.pk)
        self.assertIsNone(second.data["next"])

    def test_success_is_public_and_confirms_payment_idempotently(self):
        self.mock_retrieve(self.session_data())
        for _ in range(2):
            response = self.client.get(SUCCESS_URL, {"session_id": "cs_test_current"})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["status"], "PAID")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PAID)
        self.assertEqual(Payment.objects.count(), 2)

    def test_success_does_not_confirm_unpaid_session(self):
        self.mock_retrieve(self.session_data(
            status="complete", payment_status="unpaid",
        ))
        response = self.client.get(SUCCESS_URL, {"session_id": "cs_test_current"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "PENDING")

    def test_success_rejects_missing_session_id_before_contacting_stripe(self):
        response = self.client.get(SUCCESS_URL)
        self.assertEqual(response.status_code, 400)
        self.assertIn("session_id", response.data)
        self.factory.assert_not_called()

    def test_success_maps_stripe_errors_to_api_responses(self):
        for error, expected in (
            (stripe.InvalidRequestError("Unknown session", "id"), 400),
            (stripe.APIConnectionError("Unavailable"), 503),
        ):
            with self.subTest(expected=expected):
                self.sessions.retrieve.side_effect = error
                response = self.client.get(
                    SUCCESS_URL, {"session_id": "cs_test_unknown"},
                )
                self.assertEqual(response.status_code, expected)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    def test_cancel_is_public_and_does_not_change_payment_or_inventory(self):
        response = self.client.get(reverse("payments:cancel"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "cancelled")
        self.payment.refresh_from_db()
        self.book.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertEqual(self.book.inventory, 3)
        self.factory.assert_not_called()

    def test_signed_completed_and_async_succeeded_webhooks_confirm_payment(self):
        for event_type in (
            "checkout.session.completed", "checkout.session.async_payment_succeeded",
        ):
            with self.subTest(event_type=event_type):
                Payment.objects.filter(pk=self.payment.pk).update(
                    status=Payment.Status.PENDING,
                )
                response = self.signed_webhook(event_type, self.session_data())
                self.assertEqual(response.status_code, 200, response.data)
                self.payment.refresh_from_db()
                self.assertEqual(self.payment.status, Payment.Status.PAID)
        self.factory.assert_not_called()

    def test_repeated_webhook_does_not_duplicate_payment_or_change_stock(self):
        data = self.session_data()
        for _ in range(2):
            response = self.signed_webhook("checkout.session.completed", data)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.objects.count(), 2)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PAID)

    def test_expired_webhook_marks_pending_payment_but_cannot_downgrade_paid(self):
        expired = self.session_data(status="expired", payment_status="unpaid")
        response = self.signed_webhook("checkout.session.expired", expired)
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.EXPIRED)
        response = self.signed_webhook(
            "checkout.session.completed", self.session_data(),
        )
        self.assertEqual(response.status_code, 200)
        response = self.signed_webhook("checkout.session.expired", expired)
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PAID)

    def test_missing_or_invalid_webhook_signature_is_rejected(self):
        for signature in ("", "t=1,v1=invalid"):
            with self.subTest(signature=signature):
                response = self.client.post(
                    WEBHOOK_URL, b'{}', content_type="application/json",
                    HTTP_STRIPE_SIGNATURE=signature,
                )
                self.assertEqual(response.status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    def test_signed_webhook_with_wrong_amount_cannot_mark_payment_paid(self):
        response = self.signed_webhook(
            "checkout.session.completed", self.session_data(amount_total=1),
        )
        self.assertEqual(response.status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertIsNone(self.payment.session_id)

    def test_unrelated_events_and_sessions_without_payment_metadata_are_ignored(self):
        for event_type, data in (
            ("checkout.session.completed", self.session_data(metadata={})),
            ("payment_intent.succeeded", {"object": "payment_intent", "id": "pi_test"}),
        ):
            with self.subTest(event_type=event_type):
                response = self.signed_webhook(event_type, data)
                self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_unconfigured_webhook_returns_503(self):
        response = self.client.post(WEBHOOK_URL, {}, format="json")
        self.assertEqual(response.status_code, 503)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
