from decimal import Decimal

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from books.models import Book
from borrowings.models import Borrowing
from users.models import User
from users.roles import EMPLOYEE, assign_role


BOOKS_URL = reverse("books:books-list")


def detail_url(book: Book) -> str:
    return reverse("books:books-detail", args=[book.pk])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class BookApiTests(APITestCase):
    client: APIClient
    customer: User
    employee: User
    admin: User
    available: Book
    sold_out: Book
    inactive: Book
    inactive_empty: Book

    @classmethod
    def setUpTestData(cls) -> None:
        cls.customer = User.objects.create_user("reader@example.com")
        cls.employee = User.objects.create_user("employee@example.com")
        assign_role(cls.employee, EMPLOYEE)
        cls.admin = User.objects.create_superuser("admin@example.com", "Password!2026")
        cls.available = Book.objects.create(
            title="Alpha Hobbit", author="J. R. R. Tolkien", cover="HARD",
            inventory=3, daily_fee=Decimal("1.50"),
        )
        cls.sold_out = Book.objects.create(
            title="Beta Hobbit", author="Another Author", cover="SOFT",
            inventory=0, daily_fee=Decimal("2.00"),
        )
        cls.inactive = Book.objects.create(
            title="Gamma Archive", author="J. R. R. Tolkien", cover="HARD",
            inventory=2, daily_fee=Decimal("1.00"), is_active=False,
        )
        cls.inactive_empty = Book.objects.create(
            title="Omega Archive", author="Another Author", cover="SOFT",
            inventory=0, daily_fee=Decimal("0.00"), is_active=False,
        )

    def setUp(self) -> None:
        self.payload: dict[str, object] = {
            "title": "New Book", "author": "New Author", "cover": "SOFT",
            "inventory": 5, "daily_fee": "2.50", "is_active": True,
        }

    def test_anonymous_cannot_read_or_write_catalog(self):
        self.assertEqual(self.client.get(BOOKS_URL).status_code, 401)
        self.assertEqual(self.client.get(detail_url(self.available)).status_code, 401)
        self.assertEqual(self.client.post(
            BOOKS_URL, self.payload, format="json",
        ).status_code, 401)
        for method in (self.client.put, self.client.patch, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(
                    detail_url(self.available), self.payload, format="json",
                )
                self.assertEqual(response.status_code, 401)
        self.assertEqual(Book.objects.count(), 4)
        self.available.refresh_from_db()
        self.assertEqual(self.available.title, "Alpha Hobbit")

    def test_customer_list_includes_active_books_even_with_zero_inventory(self):
        self.client.force_authenticate(self.customer)
        response = self.client.get(BOOKS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual([book["id"] for book in response.data["results"]],
                         [self.available.pk, self.sold_out.pk])

    def test_employee_and_admin_list_includes_inactive_books(self):
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                response = self.client.get(BOOKS_URL)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["count"], 4)
                self.assertEqual([book["id"] for book in response.data["results"]], [
                    self.available.pk, self.sold_out.pk,
                    self.inactive.pk, self.inactive_empty.pk,
                ])

    def test_all_roles_can_retrieve_active_book(self):
        for user in (self.customer, self.employee, self.admin):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                response = self.client.get(detail_url(self.available))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["id"], self.available.pk)
                self.assertEqual(response.data["daily_fee"], "1.50")

    def test_only_employee_and_admin_can_retrieve_inactive_book(self):
        cases: tuple[tuple[User, int], ...] = (
            (self.customer, 404), (self.employee, 200), (self.admin, 200),
        )
        for user, expected in cases:
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                response = self.client.get(detail_url(self.inactive))
                self.assertEqual(response.status_code, expected)

    def test_customer_cannot_create_update_or_delete_books(self):
        self.client.force_authenticate(self.customer)
        response = self.client.post(BOOKS_URL, self.payload, format="json")
        self.assertEqual(response.status_code, 403)
        for method in (self.client.put, self.client.patch, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(
                    detail_url(self.available), self.payload, format="json",
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(Book.objects.count(), 4)
        self.available.refresh_from_db()
        self.assertEqual(self.available.title, "Alpha Hobbit")
        self.assertEqual(self.available.inventory, 3)

    def test_employee_and_admin_can_create_books(self):
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                self.client.force_authenticate(user)
                response = self.client.post(BOOKS_URL, self.payload, format="json")
                self.assertEqual(response.status_code, 201, response.data)
                book = Book.objects.get(pk=response.data["id"])
                self.assertEqual(book.title, "New Book")
                self.assertEqual(book.inventory, 5)
                self.assertEqual(book.daily_fee, Decimal("2.50"))
        self.assertEqual(Book.objects.count(), 6)

    def test_employee_and_admin_can_replace_book(self):
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                book = Book.objects.create(**self.payload)
                self.client.force_authenticate(user)
                response = self.client.put(detail_url(book), {
                    "title": "Replacement", "author": "Changed Author",
                    "cover": "HARD", "inventory": 0, "daily_fee": "0.00",
                    "is_active": False,
                }, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                book.refresh_from_db()
                self.assertEqual(book.title, "Replacement")
                self.assertEqual(book.author, "Changed Author")
                self.assertEqual(book.cover, "HARD")
                self.assertEqual(book.inventory, 0)
                self.assertEqual(book.daily_fee, Decimal("0.00"))
                self.assertFalse(book.is_active)

    def test_employee_and_admin_can_patch_inventory(self):
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                book = Book.objects.create(**self.payload)
                self.client.force_authenticate(user)
                response = self.client.patch(detail_url(book), {
                    "inventory": 0,
                }, format="json")
                self.assertEqual(response.status_code, 200)
                book.refresh_from_db()
                self.assertEqual(book.inventory, 0)
                self.assertEqual(book.title, "New Book")
                self.assertTrue(book.is_active)

    def test_invalid_create_or_update_does_not_change_database(self):
        self.client.force_authenticate(self.employee)
        payload = self.payload.copy()
        payload["inventory"] = -1
        response = self.client.post(BOOKS_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("inventory", response.data)
        response = self.client.patch(detail_url(self.available), {
            "title": "Should not be saved", "daily_fee": "-1.00",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("daily_fee", response.data)
        self.available.refresh_from_db()
        self.assertEqual(self.available.title, "Alpha Hobbit")
        self.assertEqual(self.available.daily_fee, Decimal("1.50"))
        self.assertEqual(Book.objects.count(), 4)

    def test_employee_and_admin_can_delete_book_without_borrowings(self):
        for user in (self.employee, self.admin):
            with self.subTest(user=user.email):
                book = Book.objects.create(**self.payload)
                self.client.force_authenticate(user)
                response = self.client.delete(detail_url(book))
                self.assertEqual(response.status_code, 204)
                self.assertFalse(Book.objects.filter(pk=book.pk).exists())

    def test_active_and_returned_borrowings_prevent_book_deletion(self):
        today = timezone.localdate()
        for user in (self.employee, self.admin):
            for returned in (False, True):
                with self.subTest(user=user.email, returned=returned):
                    book = Book.objects.create(**self.payload)
                    borrowing = Borrowing.objects.create(
                        book=book, user=self.customer, borrow_date=today,
                        expected_return_date=today, daily_fee=book.daily_fee,
                        actual_return_date=today if returned else None,
                    )
                    self.client.force_authenticate(user)
                    response = self.client.delete(detail_url(book))
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn("detail", response.data)
                    self.assertTrue(Book.objects.filter(pk=book.pk).exists())
                    self.assertTrue(Borrowing.objects.filter(pk=borrowing.pk).exists())
                    book.refresh_from_db()
                    self.assertEqual(book.inventory, 5)

    def test_book_with_borrowing_can_be_withdrawn_from_catalog(self):
        today = timezone.localdate()
        borrowing = Borrowing.objects.create(
            book=self.available, user=self.customer,
            expected_return_date=today, daily_fee=self.available.daily_fee,
        )
        self.client.force_authenticate(self.employee)
        response = self.client.patch(detail_url(self.available), {
            "is_active": False,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.available.refresh_from_db()
        self.assertFalse(self.available.is_active)
        self.assertEqual(self.available.inventory, 3)
        self.assertTrue(Borrowing.objects.filter(pk=borrowing.pk).exists())
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.get(detail_url(self.available)).status_code, 404)

    def test_title_and_author_filters_are_case_insensitive_partial_matches(self):
        self.client.force_authenticate(self.employee)
        cases = (
            ({"title": "hObBi"}, {self.available.pk, self.sold_out.pk}),
            ({"author": "tOLki"}, {self.available.pk, self.inactive.pk}),
        )
        for params, expected_ids in cases:
            with self.subTest(params=params):
                response = self.client.get(BOOKS_URL, params)
                self.assertEqual(response.status_code, 200)
                self.assertSetEqual(
                    {book["id"] for book in response.data["results"]}, expected_ids,
                )

    def test_cover_filter(self):
        self.client.force_authenticate(self.employee)
        response = self.client.get(BOOKS_URL, {"cover": "SOFT"})
        self.assertEqual(response.status_code, 200)
        self.assertSetEqual({book["id"] for book in response.data["results"]},
                            {self.sold_out.pk, self.inactive_empty.pk})

    def test_is_active_filter_handles_both_boolean_values(self):
        self.client.force_authenticate(self.employee)
        for value, expected_ids in (
            ("true", {self.available.pk, self.sold_out.pk}),
            ("false", {self.inactive.pk, self.inactive_empty.pk}),
        ):
            with self.subTest(value=value):
                response = self.client.get(BOOKS_URL, {"is_active": value})
                self.assertEqual(response.status_code, 200)
                self.assertSetEqual(
                    {book["id"] for book in response.data["results"]}, expected_ids,
                )

    def test_available_filter_accounts_for_inventory_and_catalog_status(self):
        self.client.force_authenticate(self.employee)
        for value, expected_ids in (
            ("true", {self.available.pk}),
            ("false", {self.sold_out.pk, self.inactive.pk, self.inactive_empty.pk}),
        ):
            with self.subTest(value=value):
                response = self.client.get(BOOKS_URL, {"available": value})
                self.assertEqual(response.status_code, 200)
                self.assertSetEqual(
                    {book["id"] for book in response.data["results"]}, expected_ids,
                )

    def test_customer_filters_cannot_expose_inactive_books(self):
        self.client.force_authenticate(self.customer)
        for params, expected_ids in (
            ({"is_active": "false"}, set()),
            ({"available": "false"}, {self.sold_out.pk}),
            ({"author": "Tolkien"}, {self.available.pk}),
        ):
            with self.subTest(params=params):
                response = self.client.get(BOOKS_URL, params)
                self.assertEqual(response.status_code, 200)
                self.assertSetEqual(
                    {book["id"] for book in response.data["results"]}, expected_ids,
                )

    def test_combined_filters_intersect(self):
        self.client.force_authenticate(self.employee)
        response = self.client.get(BOOKS_URL, {
            "title": "Hobbit", "author": "Another", "cover": "SOFT",
            "is_active": "true", "available": "false",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([book["id"] for book in response.data["results"]],
                         [self.sold_out.pk])

    def test_invalid_filters_return_field_errors(self):
        self.client.force_authenticate(self.employee)
        for field, value in (
            ("available", "maybe"), ("is_active", "maybe"),
            ("cover", "NONE"), ("title", " "), ("author", " "),
        ):
            with self.subTest(field=field):
                response = self.client.get(BOOKS_URL, {field: value})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_unmatched_filter_returns_empty_paginated_result(self):
        self.client.force_authenticate(self.customer)
        response = self.client.get(BOOKS_URL, {"title": "Missing title"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_pagination_preserves_order_and_does_not_repeat_books(self):
        self.client.force_authenticate(self.employee)
        first = self.client.get(BOOKS_URL, {"page_size": 2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], 4)
        self.assertIsNone(first.data["previous"])
        self.assertIsNotNone(first.data["next"])
        second = self.client.get(first.data["next"])
        self.assertEqual(second.status_code, 200)
        self.assertIsNone(second.data["next"])
        self.assertIsNotNone(second.data["previous"])
        ids = [book["id"] for book in first.data["results"] + second.data["results"]]
        self.assertEqual(ids, [
            self.available.pk, self.sold_out.pk,
            self.inactive.pk, self.inactive_empty.pk,
        ])

    def test_default_and_maximum_page_sizes(self):
        Book.objects.bulk_create([
            Book(title=f"Extra {index:03}", author="Author", inventory=1,
                 daily_fee=Decimal("1.00"))
            for index in range(101)
        ])
        self.client.force_authenticate(self.employee)
        response = self.client.get(BOOKS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 105)
        self.assertEqual(len(response.data["results"]), 10)
        response = self.client.get(BOOKS_URL, {"page_size": 1000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 100)
        self.assertIsNotNone(response.data["next"])

    def test_missing_book_and_page_return_404(self):
        self.client.force_authenticate(self.employee)
        response = self.client.get(reverse("books:books-detail", args=[999999]))
        self.assertEqual(response.status_code, 404)
        response = self.client.get(BOOKS_URL, {"page": 999999})
        self.assertEqual(response.status_code, 404)
