from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from users.models import User
from users.roles import ADMIN, CUSTOMER, EMPLOYEE, assign_role, get_role


PASSWORD = "N7!StrongLibraryPassword"
NEW_PASSWORD = "Different!Secret2026"
PROFILE_URL = reverse("users:manage")
PASSWORD_URL = reverse("users:change-password")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProfileTests(APITestCase):
    client: APIClient
    customer: User
    employee: User
    admin: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.customer = User.objects.create_user("customer@example.com", PASSWORD)
        cls.employee = User.objects.create_user("employee@example.com", PASSWORD)
        assign_role(cls.employee, EMPLOYEE)
        cls.admin = User.objects.create_superuser("admin@example.com", PASSWORD)

    def test_all_roles_read_only_their_own_profile_without_password(self):
        for user in (self.customer, self.employee, self.admin):
            with self.subTest(role=get_role(user)):
                self.client.force_authenticate(user)
                response = self.client.get(PROFILE_URL)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["id"], user.pk)
                self.assertEqual(response.data["role"], get_role(user))
                self.assertNotIn("password", response.data)

    def test_customer_and_admin_can_patch_profile(self):
        for user in (self.customer, self.admin):
            with self.subTest(role=get_role(user)):
                self.client.force_authenticate(user)
                response = self.client.patch(PROFILE_URL, {
                    "first_name": "Updated", "email": f" NEW{user.pk}@EXAMPLE.COM ",
                }, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                user.refresh_from_db()
                self.assertEqual(user.first_name, "Updated")
                self.assertEqual(user.email, f"new{user.pk}@example.com")
                self.assertTrue(user.check_password(PASSWORD))

    def test_customer_can_replace_profile_with_put(self):
        self.client.force_authenticate(self.customer)
        response = self.client.put(PROFILE_URL, {
            "email": self.customer.email, "first_name": "Anna", "last_name": "Reader",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.last_name, "Reader")

    def test_employee_cannot_patch_or_put_profile(self):
        self.client.force_authenticate(self.employee)
        for method in (self.client.patch, self.client.put):
            with self.subTest(method=method.__name__):
                response = method(PROFILE_URL, {
                    "email": "changed@example.com", "first_name": "Changed",
                }, format="json")
                self.assertEqual(response.status_code, 403)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.email, "employee@example.com")
        self.assertEqual(self.employee.first_name, "")

    def test_customer_cannot_change_privileges_or_other_user(self):
        self.client.force_authenticate(self.customer)
        response = self.client.patch(PROFILE_URL, {
            "id": self.employee.pk, "role": ADMIN, "is_staff": True,
            "is_superuser": True, "groups": [], "is_active": False,
            "first_name": "Own profile",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.customer.refresh_from_db()
        self.employee.refresh_from_db()
        self.assertEqual(get_role(self.customer), CUSTOMER)
        self.assertFalse(self.customer.is_superuser)
        self.assertFalse(self.customer.is_staff)
        self.assertTrue(self.customer.is_active)
        self.assertEqual(self.customer.first_name, "Own profile")
        self.assertEqual(self.employee.first_name, "")

    def test_email_update_checks_uniqueness_but_allows_own_email(self):
        self.client.force_authenticate(self.customer)
        response = self.client.patch(PROFILE_URL, {
            "email": " EMPLOYEE@EXAMPLE.COM ",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, "customer@example.com")
        response = self.client.patch(PROFILE_URL, {
            "email": " CUSTOMER@EXAMPLE.COM ",
        }, format="json")
        self.assertEqual(response.status_code, 200)

    def test_password_cannot_be_changed_through_profile_by_any_role(self):
        cases: tuple[tuple[User, int], ...] = (
            (self.customer, 400), (self.employee, 403), (self.admin, 400),
        )
        for user, expected in cases:
            for method in (self.client.patch, self.client.put):
                with self.subTest(role=get_role(user), method=method.__name__):
                    self.client.force_authenticate(user)
                    response = method(PROFILE_URL, {
                        "email": user.email, "password": NEW_PASSWORD,
                    }, format="json")
                    self.assertEqual(response.status_code, expected)
                    user.refresh_from_db()
                    self.assertTrue(user.check_password(PASSWORD))

    def test_profile_cannot_be_deleted(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(PROFILE_URL).status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_user_without_role_cannot_edit_profile(self):
        self.customer.groups.clear()
        self.client.force_authenticate(self.customer)
        response = self.client.patch(PROFILE_URL, {"first_name": "Changed"})
        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_read_or_change_profiles(self):
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 401)
        self.assertEqual(self.client.patch(PROFILE_URL, {}).status_code, 401)
        self.assertEqual(self.client.post(PASSWORD_URL, {}).status_code, 401)
        url = reverse("users:customer-detail", args=[self.customer.pk])
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_customer_detail_access_matrix(self):
        for actor in (self.customer, self.employee, self.admin):
            self.client.force_authenticate(actor)
            for target in (self.customer, self.employee, self.admin):
                with self.subTest(actor=get_role(actor), target=get_role(target)):
                    url = reverse("users:customer-detail", args=[target.pk])
                    response = self.client.get(url)
                    if actor == self.customer:
                        expected = 403
                    elif actor == self.employee and target != self.customer:
                        expected = 404
                    else:
                        expected = 200
                    self.assertEqual(response.status_code, expected)
                    if expected == 200:
                        self.assertEqual(response.data["id"], target.pk)
                        self.assertSetEqual(set(response.data), {
                            "id", "email", "first_name", "last_name",
                        })

    def test_employee_cannot_read_mixed_role_account_as_customer(self):
        self.customer.groups.add(self.employee.groups.get(name="Employees"))
        self.client.force_authenticate(self.employee)
        url = reverse("users:customer-detail", args=[self.customer.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_customer_detail_is_read_only_even_for_admin(self):
        url = reverse("users:customer-detail", args=[self.customer.pk])
        for actor in (self.employee, self.admin):
            self.client.force_authenticate(actor)
            for method in (self.client.patch, self.client.put, self.client.delete):
                with self.subTest(actor=get_role(actor), method=method.__name__):
                    response = method(url, {"first_name": "Changed"}, format="json")
                    self.assertEqual(response.status_code, 405)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.first_name, "")

    def test_missing_customer_returns_404(self):
        self.client.force_authenticate(self.admin)
        url = reverse("users:customer-detail", args=[999999])
        self.assertEqual(self.client.get(url).status_code, 404)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PasswordChangeTests(APITestCase):
    client: APIClient
    user: User

    def setUp(self) -> None:
        self.user = User.objects.create_user("reader@example.com", PASSWORD)
        self.client.force_authenticate(self.user)

    def test_all_roles_can_change_password_with_current_password(self):
        for role in (CUSTOMER, EMPLOYEE, ADMIN):
            with self.subTest(role=role):
                assign_role(self.user, role)
                response = self.client.post(PASSWORD_URL, {
                    "old_password": PASSWORD, "new_password": NEW_PASSWORD,
                }, format="json")
                self.assertEqual(response.status_code, 204, response.data)
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(NEW_PASSWORD))
                self.assertFalse(self.user.check_password(PASSWORD))
                self.assertEqual(get_role(self.user), role)
                self.user.set_password(PASSWORD)
                self.user.save()

    def test_invalid_password_change_preserves_current_password(self):
        cases: tuple[tuple[dict[str, str], str], ...] = (
            ({"old_password": "wrong", "new_password": NEW_PASSWORD}, "old_password"),
            ({"new_password": NEW_PASSWORD}, "old_password"),
            ({"old_password": PASSWORD}, "new_password"),
            ({"old_password": PASSWORD, "new_password": "123"}, "new_password"),
            ({"old_password": PASSWORD, "new_password": "password"}, "new_password"),
            ({"old_password": PASSWORD, "new_password": "12345678901"}, "new_password"),
        )
        for payload, field in cases:
            with self.subTest(field=field, payload=payload):
                response = self.client.post(PASSWORD_URL, payload, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(PASSWORD))

    def test_password_change_updates_login_credentials(self):
        response = self.client.post(PASSWORD_URL, {
            "old_password": PASSWORD, "new_password": NEW_PASSWORD,
        }, format="json")
        self.assertEqual(response.status_code, 204)
        self.client.force_authenticate(user=None)
        cases: tuple[tuple[str, int], ...] = ((PASSWORD, 401), (NEW_PASSWORD, 200))
        for password, expected in cases:
            with self.subTest(expected=expected):
                response = self.client.post(reverse("users:token-obtain"), {
                    "email": self.user.email, "password": password,
                }, format="json")
                self.assertEqual(response.status_code, expected)

    def test_password_change_preserves_browser_session(self):
        self.client.force_authenticate(user=None)
        self.assertTrue(self.client.login(email=self.user.email, password=PASSWORD))
        response = self.client.post(PASSWORD_URL, {
            "old_password": PASSWORD, "new_password": NEW_PASSWORD,
        }, format="json")
        self.assertEqual(response.status_code, 204, response.data)
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_password_change_preserves_whitespace(self):
        new_password = f" {NEW_PASSWORD} "
        response = self.client.post(PASSWORD_URL, {
            "old_password": PASSWORD, "new_password": new_password,
        }, format="json")
        self.assertEqual(response.status_code, 204)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertFalse(self.user.check_password(new_password.strip()))
