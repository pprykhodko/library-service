from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from books.models import Book
from borrowings.models import Borrowing
from payments.models import Payment
from users.models import User
from users.roles import EMPLOYEE, assign_role

BORROWINGS_URL = reverse("borrowings:borrowings-list")


def detail_url(borrowing: Borrowing) -> str:
    return reverse("borrowings:borrowings-detail", args=[borrowing.pk])


def return_url(borrowing: Borrowing) -> str:
    return reverse("borrowings:borrowings-return-book", args=[borrowing.pk])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class BorrowingApiTests(APITestCase):
    client: APIClient
    customer: User
    other_customer: User
    employee: User
    admin: User
    book: Book

    @classmethod
    def setUpTestData(cls) -> None:
        cls.customer = User.objects.create_user("reader@example.com")
        cls.other_customer = User.objects.create_user("other@example.com")
        cls.employee = User.objects.create_user("employee@example.com")
        assign_role(cls.employee, EMPLOYEE)
        cls.admin = User.objects.create_superuser("admin@example.com", "Password!2026")
        cls.book = Book.objects.create(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=3,
            daily_fee=Decimal("1.50"),
        )

    def setUp(self) -> None:
        self.today = timezone.localdate()
        self.payload: dict[str, object] = {
            "book": self.book.pk,
            "expected_return_date": (self.today + timedelta(days=3)).isoformat(),
        }

    def make_borrowing(
        self,
        user: User | None = None,
        *,
        returned: bool = False,
        overdue: bool = False,
        fee: Decimal | None = Decimal("1.50"),
    ) -> Borrowing:
        # Existing records are fixtures; inventory is the currently available stock.
        return Borrowing.objects.create(
            user=user or self.customer,
            book=self.book,
            borrow_date=self.today - timedelta(days=3),
            expected_return_date=(
                self.today - timedelta(days=1) if overdue else self.today
            ),
            actual_return_date=self.today if returned else None,
            daily_fee=fee,
        )

    def test_anonymous_cannot_list_retrieve_create_or_return(self):
        borrowing = self.make_borrowing()
        self.assertEqual(self.client.get(BORROWINGS_URL).status_code, 401)
        self.assertEqual(self.client.get(detail_url(borrowing)).status_code, 401)
        self.assertEqual(
            self.client.post(
                BORROWINGS_URL,
                self.payload,
                format="json",
            ).status_code,
            401,
        )
        self.assertEqual(self.client.post(return_url(borrowing)).status_code, 401)
        self.book.refresh_from_db()
        borrowing.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)
        self.assertIsNone(borrowing.actual_return_date)
        self.assertEqual(Borrowing.objects.count(), 1)
        self.assertFalse(Payment.objects.exists())

    def test_customer_list_contains_only_own_active_and_returned_borrowings(self):
        active = self.make_borrowing()
        returned = self.make_borrowing(returned=True)
        self.make_borrowing(user=self.other_customer)
        self.client.force_authenticate(self.customer)
        response = self.client.get(BORROWINGS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [item["id"] for item in response.data["results"]], [returned.pk, active.pk]
        )
        self.assertEqual(response.data["results"][0]["book"], self.book.title)
        self.assertEqual(response.data["results"][0]["user"], self.customer.email)

    def test_customer_can_retrieve_own_but_not_other_users_borrowing(self):
        own = self.make_borrowing()
        other = self.make_borrowing(user=self.other_customer)
        self.client.force_authenticate(self.customer)
        response = self.client.get(detail_url(own))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["book"]["id"], self.book.pk)
        self.assertEqual(response.data["user"]["id"], self.customer.pk)
        self.assertNotIn("password", response.data["user"])
        self.assertEqual(self.client.get(detail_url(other)).status_code, 404)

    def test_customer_cannot_expose_other_users_records_with_filters(self):
        self.make_borrowing()
        self.make_borrowing(user=self.other_customer)
        self.client.force_authenticate(self.customer)
        response = self.client.get(
            BORROWINGS_URL,
            {
                "user_id": self.other_customer.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_employee_and_admin_can_list_and_retrieve_all_borrowings(self):
        own = self.make_borrowing()
        other = self.make_borrowing(user=self.other_customer)
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                response = self.client.get(BORROWINGS_URL)
                self.assertEqual(response.status_code, 200)
                self.assertSetEqual(
                    {item["id"] for item in response.data["results"]},
                    {own.pk, other.pk},
                )
                self.assertEqual(self.client.get(detail_url(other)).status_code, 200)

    def test_user_without_role_cannot_access_borrowings(self):
        borrowing = self.make_borrowing()
        self.customer.groups.clear()
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.get(BORROWINGS_URL).status_code, 403)
        self.assertEqual(self.client.get(detail_url(borrowing)).status_code, 403)
        self.assertEqual(
            self.client.post(
                BORROWINGS_URL,
                self.payload,
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post(return_url(borrowing)).status_code, 403)

    def test_customer_and_admin_can_borrow_only_for_themselves(self):
        for user in (self.customer, self.admin):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                payload = self.payload.copy()
                payload.update(
                    {
                        "user": self.other_customer.pk,
                        "daily_fee": "0.00",
                        "actual_return_date": self.today.isoformat(),
                        "borrow_date": "2000-01-01",
                        "payments": [{"status": "PAID"}],
                    }
                )
                response = self.client.post(BORROWINGS_URL, payload, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                borrowing = Borrowing.objects.get(pk=response.data["id"])
                self.assertEqual(borrowing.user_id, user.pk)
                self.assertEqual(borrowing.borrow_date, self.today)
                self.assertIsNone(borrowing.actual_return_date)
                self.assertEqual(borrowing.daily_fee, Decimal("1.50"))
                payment = borrowing.payments.get()
                self.assertEqual(payment.money_to_pay, Decimal("4.50"))
                self.assertEqual(payment.status, Payment.Status.PENDING)
                self.assertEqual(response.data["payments"][0]["id"], payment.pk)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 1)

    def test_employee_cannot_create_borrowing(self):
        self.client.force_authenticate(self.employee)
        response = self.client.post(BORROWINGS_URL, self.payload, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)

    def test_invalid_create_does_not_change_stock_or_create_payment(self):
        self.client.force_authenticate(self.customer)
        self.payload["expected_return_date"] = (
            self.today - timedelta(days=1)
        ).isoformat()
        response = self.client.post(BORROWINGS_URL, self.payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("expected_return_date", response.data)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)
        self.assertFalse(Borrowing.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_unpaid_rentals_and_fines_block_new_borrowings(self):
        existing = self.make_borrowing(returned=True)
        self.client.force_authenticate(self.customer)
        for payment_type in (Payment.Type.PAYMENT, Payment.Type.FINE):
            for status in (Payment.Status.PENDING, Payment.Status.EXPIRED):
                with self.subTest(payment_type=payment_type, status=status):
                    payment = Payment.objects.create(
                        borrowing=existing,
                        type=payment_type,
                        status=status,
                        money_to_pay=Decimal("1.50"),
                    )
                    response = self.client.post(
                        BORROWINGS_URL,
                        self.payload,
                        format="json",
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("detail", response.data)
                    self.assertEqual(Borrowing.objects.count(), 1)
                    self.assertEqual(Payment.objects.count(), 1)
                    self.book.refresh_from_db()
                    self.assertEqual(self.book.inventory, 3)
                    payment.delete()

    def test_general_update_and_delete_are_unavailable_even_to_admin(self):
        borrowing = self.make_borrowing()
        self.client.force_authenticate(self.admin)
        for method in (self.client.put, self.client.patch, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(
                    detail_url(borrowing),
                    {
                        "actual_return_date": self.today.isoformat(),
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, 405)
        borrowing.refresh_from_db()
        self.assertIsNone(borrowing.actual_return_date)

    def test_customer_cannot_confirm_own_or_other_users_return(self):
        own = self.make_borrowing()
        other = self.make_borrowing(user=self.other_customer)
        self.client.force_authenticate(self.customer)
        for borrowing in (own, other):
            with self.subTest(borrowing_id=borrowing.pk):
                response = self.client.post(return_url(borrowing))
                self.assertEqual(response.status_code, 403)
                borrowing.refresh_from_db()
                self.assertIsNone(borrowing.actual_return_date)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 3)

    def test_employee_and_admin_return_books_without_fine_on_due_date(self):
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                borrowing = self.make_borrowing()
                self.client.force_authenticate(user)
                response = self.client.post(return_url(borrowing))
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(
                    response.data["actual_return_date"], self.today.isoformat()
                )
                self.assertEqual(response.data["book"]["id"], self.book.pk)
                borrowing.refresh_from_db()
                self.assertEqual(borrowing.actual_return_date, self.today)
                self.assertFalse(borrowing.payments.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 5)

    def test_return_uses_saved_rate_and_ignores_client_supplied_date(self):
        borrowing = self.make_borrowing(overdue=True)
        Book.objects.filter(pk=self.book.pk).update(daily_fee=Decimal("9.00"))
        self.client.force_authenticate(self.employee)
        response = self.client.post(
            return_url(borrowing),
            {
                "actual_return_date": borrowing.expected_return_date.isoformat(),
                "daily_fee": "0.00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["actual_return_date"], self.today.isoformat())
        fine = borrowing.payments.get(type=Payment.Type.FINE)
        self.assertEqual(fine.money_to_pay, Decimal("3.00"))
        self.assertEqual(fine.status, Payment.Status.PENDING)
        self.assertEqual(response.data["payments"][0]["id"], fine.pk)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 4)

    def test_repeated_return_cannot_duplicate_fine_or_stock(self):
        borrowing = self.make_borrowing(overdue=True)
        self.client.force_authenticate(self.employee)
        self.assertEqual(self.client.post(return_url(borrowing)).status_code, 200)
        fine_id = borrowing.payments.get().pk
        response = self.client.post(return_url(borrowing))

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.data)
        self.assertEqual(borrowing.payments.count(), 1)
        self.assertEqual(borrowing.payments.get().pk, fine_id)
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 4)

    def test_minimum_nonzero_fine_is_fifty_cents(self):
        borrowing = self.make_borrowing(overdue=True, fee=Decimal("0.10"))
        self.client.force_authenticate(self.employee)
        response = self.client.post(return_url(borrowing))
        self.assertEqual(response.status_code, 200)
        fine = borrowing.payments.get(type=Payment.Type.FINE)
        self.assertEqual(fine.money_to_pay, Decimal("0.50"))

    def test_overdue_free_borrowing_does_not_create_fine(self):
        borrowing = self.make_borrowing(overdue=True, fee=Decimal("0.00"))
        self.client.force_authenticate(self.employee)
        self.assertEqual(self.client.post(return_url(borrowing)).status_code, 200)
        self.assertFalse(borrowing.payments.exists())
        self.book.refresh_from_db()
        self.assertEqual(self.book.inventory, 4)

    def test_fine_errors_roll_back_return_date_and_stock(self):
        self.client.force_authenticate(self.employee)
        for fee in (None, Decimal("1000000.00")):
            with self.subTest(fee=fee):
                borrowing = self.make_borrowing(overdue=True, fee=fee)
                response = self.client.post(return_url(borrowing))
                self.assertEqual(response.status_code, 400)
                borrowing.refresh_from_db()
                self.book.refresh_from_db()
                self.assertIsNone(borrowing.actual_return_date)
                self.assertEqual(self.book.inventory, 3)
                self.assertFalse(borrowing.payments.exists())

    def test_unpaid_rental_does_not_prevent_book_return(self):
        borrowing = self.make_borrowing()
        payment = Payment.objects.create(
            borrowing=borrowing,
            money_to_pay=Decimal("4.50"),
        )
        self.client.force_authenticate(self.employee)
        self.assertEqual(self.client.post(return_url(borrowing)).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(borrowing.payments.count(), 1)

    def test_future_borrowing_cannot_be_returned(self):
        tomorrow = self.today + timedelta(days=1)
        borrowing = Borrowing.objects.create(
            book=self.book,
            user=self.customer,
            borrow_date=tomorrow,
            expected_return_date=tomorrow,
            daily_fee=Decimal("1.50"),
        )
        self.client.force_authenticate(self.employee)
        response = self.client.post(return_url(borrowing))
        self.assertEqual(response.status_code, 400)
        borrowing.refresh_from_db()
        self.book.refresh_from_db()
        self.assertIsNone(borrowing.actual_return_date)
        self.assertEqual(self.book.inventory, 3)
        self.assertFalse(borrowing.payments.exists())

    def test_user_filter_does_not_implicitly_filter_out_active_records(self):
        active = self.make_borrowing()
        returned = self.make_borrowing(returned=True)
        self.make_borrowing(user=self.other_customer)
        self.client.force_authenticate(self.employee)
        response = self.client.get(BORROWINGS_URL, {"user_id": self.customer.pk})
        self.assertEqual(response.status_code, 200)
        self.assertSetEqual(
            {item["id"] for item in response.data["results"]}, {active.pk, returned.pk}
        )

    def test_active_filter_handles_true_and_false_with_user_filter(self):
        active = self.make_borrowing()
        returned = self.make_borrowing(returned=True)
        self.make_borrowing(user=self.other_customer)
        self.make_borrowing(user=self.other_customer, returned=True)
        self.client.force_authenticate(self.employee)
        for value, expected_id in (("true", active.pk), ("false", returned.pk)):
            with self.subTest(value=value):
                response = self.client.get(
                    BORROWINGS_URL,
                    {
                        "user_id": self.customer.pk,
                        "is_active": value,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [item["id"] for item in response.data["results"]], [expected_id]
                )

    def test_invalid_filters_return_400(self):
        self.client.force_authenticate(self.customer)
        for field, value in (
            ("user_id", "0"),
            ("user_id", "abc"),
            ("is_active", "maybe"),
        ):
            with self.subTest(field=field, value=value):
                response = self.client.get(BORROWINGS_URL, {field: value})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_pagination_orders_by_date_then_id_without_repeating_records(self):
        first = self.make_borrowing()
        older = self.make_borrowing()
        older.borrow_date -= timedelta(days=1)
        older.save(update_fields=["borrow_date"])
        latest = self.make_borrowing()
        self.client.force_authenticate(self.customer)
        page_one = self.client.get(BORROWINGS_URL, {"page_size": 2})
        self.assertEqual(page_one.status_code, 200)
        self.assertEqual(page_one.data["count"], 3)
        self.assertIsNone(page_one.data["previous"])
        self.assertIsNotNone(page_one.data["next"])
        page_two = self.client.get(page_one.data["next"])
        self.assertEqual(page_two.status_code, 200)
        self.assertIsNone(page_two.data["next"])
        self.assertIsNotNone(page_two.data["previous"])
        results = page_one.data["results"] + page_two.data["results"]
        self.assertEqual(
            [item["id"] for item in results], [latest.pk, first.pk, older.pk]
        )

    def test_default_and_maximum_page_sizes(self):
        Borrowing.objects.bulk_create(
            [
                Borrowing(
                    book=self.book,
                    user=self.customer,
                    borrow_date=self.today,
                    expected_return_date=self.today,
                    daily_fee=Decimal("1.50"),
                )
                for _ in range(101)
            ]
        )
        self.client.force_authenticate(self.customer)
        response = self.client.get(BORROWINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 101)
        self.assertEqual(len(response.data["results"]), 10)
        response = self.client.get(BORROWINGS_URL, {"page_size": 1000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 100)
        self.assertIsNotNone(response.data["next"])

    def test_missing_borrowing_and_page_return_404(self):
        self.client.force_authenticate(self.employee)
        self.assertEqual(
            self.client.get(
                reverse("borrowings:borrowings-detail", args=[999999]),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse("borrowings:borrowings-return-book", args=[999999]),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                BORROWINGS_URL,
                {"page": 999999},
            ).status_code,
            404,
        )
