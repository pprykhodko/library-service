from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from books.models import Book
from borrowings.models import Borrowing
from payments.models import Payment
from users.models import User
from users.roles import EMPLOYEE, assign_role


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PaymentAdminTests(TestCase):
    admin: User
    customer: User
    employee: User
    payment: Payment
    fine: Payment

    @classmethod
    def setUpTestData(cls) -> None:
        cls.admin = User.objects.create_superuser("admin@example.com", "Password!2026")
        cls.customer = User.objects.create_user("reader@example.com")
        cls.employee = User.objects.create_user("employee@example.com")
        assign_role(cls.employee, EMPLOYEE)
        book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=3,
            daily_fee=Decimal("1.50"),
        )
        borrowing = Borrowing.objects.create(
            book=book,
            user=cls.customer,
            expected_return_date=timezone.localdate(),
            daily_fee=book.daily_fee,
        )
        cls.payment = Payment.objects.create(
            borrowing=borrowing,
            money_to_pay=Decimal("4.50"),
            session_id="cs_test_existing",
            session_url="https://checkout.stripe.com/c/pay/cs_test_existing",
        )
        cls.fine = Payment.objects.create(
            borrowing=borrowing,
            type=Payment.Type.FINE,
            status=Payment.Status.PAID,
            money_to_pay=Decimal("3.00"),
        )

    def setUp(self) -> None:
        self.list_url = reverse("admin:payments_payment_changelist")
        self.change_url = reverse(
            "admin:payments_payment_change",
            args=[self.payment.pk],
        )
        self.delete_url = reverse(
            "admin:payments_payment_delete",
            args=[self.payment.pk],
        )
        self.add_url = reverse("admin:payments_payment_add")
        self.client.force_login(self.admin)

    def test_admin_can_view_payments_and_filter_by_status_and_type(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertSetEqual(
            set(response.context["cl"].result_list), {self.payment, self.fine}
        )
        response = self.client.get(
            self.list_url,
            {
                "status__exact": "PAID",
                "type__exact": "FINE",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["cl"].result_list), [self.fine])

    def test_payment_detail_has_no_editable_fields(self):
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["adminform"].form.fields, {})
        self.assertContains(response, "cs_test_existing")

    def test_forged_post_cannot_change_payment_fields(self):
        original = Payment.objects.values().get(pk=self.payment.pk)
        response = self.client.post(
            self.change_url,
            {
                "borrowing": 999999,
                "status": "PAID",
                "type": "FINE",
                "money_to_pay": "0.00",
                "session_id": "cs_test_forged",
                "session_url": "https://example.com/forged",
                "checkout_key": "00000000-0000-0000-0000-000000000000",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.objects.values().get(pk=self.payment.pk), original)
        self.assertEqual(Payment.objects.count(), 2)

    def test_admin_cannot_create_payment(self):
        self.assertEqual(self.client.get(self.add_url).status_code, 403)
        response = self.client.post(
            self.add_url,
            {
                "borrowing": self.payment.borrowing_id,
                "status": "PAID",
                "type": "PAYMENT",
                "money_to_pay": "1.00",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Payment.objects.count(), 2)

    def test_admin_cannot_delete_payment(self):
        self.assertEqual(self.client.get(self.delete_url).status_code, 403)
        response = self.client.post(self.delete_url, {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Payment.objects.filter(pk=self.payment.pk).exists())

    def test_bulk_delete_action_cannot_remove_payments(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        action_form = response.context["action_form"]
        if action_form is not None:
            actions = dict(action_form.fields["action"].choices)
            self.assertNotIn("delete_selected", actions)
        self.client.post(
            self.list_url,
            {
                "action": "delete_selected",
                "_selected_action": [self.payment.pk],
                "post": "yes",
            },
        )
        self.assertEqual(Payment.objects.count(), 2)

    def test_customers_and_employees_cannot_access_payment_admin(self):
        for user in (self.customer, self.employee):
            self.client.force_login(user)
            for url in (self.list_url, self.change_url, self.add_url, self.delete_url):
                with self.subTest(user=user.email, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(reverse("admin:login"), response.url)
