from datetime import timedelta
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
class BorrowingAdminTests(TestCase):
    admin: User
    customer: User
    employee: User
    book: Book
    other_book: Book
    borrowing: Borrowing
    legacy_borrowing: Borrowing

    @classmethod
    def setUpTestData(cls) -> None:
        cls.admin = User.objects.create_superuser("admin@example.com", "Password!2026")
        cls.customer = User.objects.create_user("reader@example.com")
        cls.employee = User.objects.create_user("employee@example.com")
        assign_role(cls.employee, EMPLOYEE)
        cls.book = Book.objects.create(
            title="The Hobbit", author="J. R. R. Tolkien", inventory=2, daily_fee="1.50"
        )
        cls.other_book = Book.objects.create(
            title="Dune", author="Frank Herbert", inventory=4, daily_fee="2.00"
        )
        today = timezone.localdate()
        cls.borrowing = Borrowing.objects.create(
            book=cls.book,
            user=cls.customer,
            borrow_date=today - timedelta(days=5),
            expected_return_date=today - timedelta(days=2),
            daily_fee=Decimal("1.50"),
        )
        Payment.objects.create(
            borrowing=cls.borrowing,
            money_to_pay=Decimal("4.50"),
            status=Payment.Status.PAID,
        )
        # No protected payment: deletion must be denied by admin permissions.
        cls.legacy_borrowing = Borrowing.objects.create(
            book=cls.book,
            user=cls.customer,
            expected_return_date=today + timedelta(days=3),
        )

    def setUp(self) -> None:
        self.client.force_login(self.admin)
        self.list_url = reverse("admin:borrowings_borrowing_changelist")
        self.add_url = reverse("admin:borrowings_borrowing_add")
        self.change_url = reverse(
            "admin:borrowings_borrowing_change", args=[self.borrowing.pk]
        )
        self.delete_url = reverse(
            "admin:borrowings_borrowing_delete", args=[self.legacy_borrowing.pk]
        )
        self.original_state = self.database_state()

    @staticmethod
    def database_state() -> tuple:
        return (
            list(Book.objects.order_by("pk").values()),
            list(Borrowing.objects.order_by("pk").values()),
            list(Payment.objects.order_by("pk").values()),
        )

    def test_admin_can_view_list_and_details(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertSetEqual(
            set(response.context["cl"].result_list),
            {self.borrowing, self.legacy_borrowing},
        )
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.book.title)
        self.assertContains(response, self.customer.email)
        self.assertEqual(self.database_state(), self.original_state)

    def test_detail_is_read_only_even_for_superuser(self):
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["adminform"].form.fields, {})
        self.assertTrue(response.context["has_view_permission"])
        self.assertFalse(response.context["has_change_permission"])
        self.assertFalse(response.context["has_add_permission"])
        self.assertFalse(response.context["has_delete_permission"])

    def test_admin_cannot_create_borrowing(self):
        self.assertEqual(self.client.get(self.add_url).status_code, 403)
        response = self.client.post(
            self.add_url,
            {
                "book": self.book.pk,
                "user": self.customer.pk,
                "expected_return_date": timezone.localdate().isoformat(),
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database_state(), self.original_state)

    def test_forged_post_cannot_change_or_return_borrowing(self):
        response = self.client.post(
            self.change_url,
            {
                "book": self.other_book.pk,
                "user": self.employee.pk,
                "borrow_date": timezone.localdate().isoformat(),
                "expected_return_date": timezone.localdate().isoformat(),
                "actual_return_date": timezone.localdate().isoformat(),
                "daily_fee": "0.00",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database_state(), self.original_state)

    def test_admin_cannot_delete_borrowing_without_payments(self):
        self.assertEqual(self.client.get(self.delete_url).status_code, 403)
        response = self.client.post(self.delete_url, {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database_state(), self.original_state)

    def test_bulk_delete_is_unavailable_and_forged_action_cannot_delete(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        action_form = response.context["action_form"]
        if action_form is not None:
            self.assertNotIn(
                "delete_selected", dict(action_form.fields["action"].choices)
            )
        self.client.post(
            self.list_url,
            {
                "action": "delete_selected",
                "_selected_action": [self.legacy_borrowing.pk],
                "post": "yes",
            },
        )
        self.assertEqual(self.database_state(), self.original_state)

    def test_customers_employees_and_anonymous_users_cannot_access_admin(self):
        for user in (self.customer, self.employee, None):
            self.client.logout()
            if user is not None:
                self.client.force_login(user)
            for url in (self.list_url, self.change_url, self.add_url, self.delete_url):
                for method in (self.client.get, self.client.post):
                    with self.subTest(user=user, url=url, method=method.__name__):
                        response = method(url)
                        self.assertEqual(response.status_code, 302)
                        self.assertIn(reverse("admin:login"), response.url)
        self.assertEqual(self.database_state(), self.original_state)
