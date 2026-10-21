from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from users.forms import AdminUserChangeForm, AdminUserCreationForm
from users.roles import ADMIN, CUSTOMER, EMPLOYEE, assign_role, get_role

User = get_user_model()
PASSWORD = "N7!StrongLibraryPassword"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminFormTests(TestCase):
    def test_creation_form_defaults_to_customer(self):
        self.assertEqual(AdminUserCreationForm().fields["role"].initial, CUSTOMER)

    def test_change_form_initial_role_matches_user(self):
        user = User.objects.create_user("reader@example.com", PASSWORD)
        assign_role(user, EMPLOYEE)
        self.assertEqual(
            AdminUserChangeForm(instance=user).fields["role"].initial, EMPLOYEE
        )

    def test_creation_form_normalizes_email_and_hashes_password(self):
        form = AdminUserCreationForm(
            data={
                "email": " NEW@EXAMPLE.COM ",
                "role": CUSTOMER,
                "password1": PASSWORD,
                "password2": PASSWORD,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, "new@example.com")
        self.assertTrue(user.check_password(PASSWORD))

    def test_creation_form_rejects_duplicate_email_and_password_mismatch(self):
        User.objects.create_user("reader@example.com", PASSWORD)
        for overrides, field in (
            ({"email": " READER@EXAMPLE.COM "}, "email"),
            ({"password2": "Different!Password"}, "password2"),
            ({"role": "owner"}, "role"),
        ):
            with self.subTest(field=field):
                data = {
                    "email": "new@example.com",
                    "role": CUSTOMER,
                    "password1": PASSWORD,
                    "password2": PASSWORD,
                }
                data.update(overrides)
                form = AdminUserCreationForm(data=data)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UserAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin@example.com", PASSWORD)
        cls.customer = User.objects.create_user("customer@example.com", PASSWORD)
        cls.employee = User.objects.create_user("employee@example.com", PASSWORD)
        assign_role(cls.employee, EMPLOYEE)

    def test_superuser_can_list_search_and_view_users(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("admin:users_user_changelist"), {"q": "customer@example.com"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "customer@example.com")
        self.assertEqual(list(response.context["cl"].result_list), [self.customer])
        url = reverse("admin:users_user_change", args=[self.customer.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_admin_creation_assigns_selected_role(self):
        self.client.force_login(self.admin)
        for role in (CUSTOMER, EMPLOYEE, ADMIN):
            with self.subTest(role=role):
                response = self.client.post(
                    reverse("admin:users_user_add"),
                    {
                        "email": f"new-{role}@example.com",
                        "role": role,
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                        "first_name": "New",
                        "last_name": "Account",
                        "_save": "Save",
                    },
                )
                self.assertEqual(response.status_code, 302)
                user = User.objects.get(email=f"new-{role}@example.com")
                self.assertEqual(get_role(user), role)
                self.assertEqual(user.is_staff, role == ADMIN)
                self.assertEqual(user.is_superuser, role == ADMIN)
                self.assertTrue(user.check_password(PASSWORD))

    def test_admin_can_change_role_and_keep_own_email(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("admin:users_user_change", args=[self.customer.pk]),
            {
                "email": " CUSTOMER@EXAMPLE.COM ",
                "role": EMPLOYEE,
                "first_name": "Updated",
                "last_name": "Reader",
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(get_role(self.customer), EMPLOYEE)
        self.assertEqual(self.customer.email, "customer@example.com")
        self.assertTrue(self.customer.check_password(PASSWORD))

    def test_admin_cannot_change_email_to_an_existing_account(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("admin:users_user_change", args=[self.customer.pk]),
            {
                "email": " EMPLOYEE@EXAMPLE.COM ",
                "role": CUSTOMER,
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["adminform"].form.errors)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, "customer@example.com")

    def test_customers_and_employees_cannot_access_user_admin(self):
        for user in (self.customer, self.employee):
            self.client.force_login(user)
            for url in (
                reverse("admin:users_user_changelist"),
                reverse("admin:users_user_add"),
                reverse("admin:users_user_change", args=[self.customer.pk]),
                reverse("admin:users_user_delete", args=[self.customer.pk]),
            ):
                with self.subTest(role=get_role(user), url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn(reverse("admin:login"), response.url)

    def test_staff_flag_alone_does_not_grant_access_to_user_admin(self):
        self.employee.is_staff = True
        self.employee.save()
        self.client.force_login(self.employee)
        for url in (
            reverse("admin:users_user_changelist"),
            reverse("admin:users_user_add"),
            reverse("admin:users_user_change", args=[self.customer.pk]),
            reverse("admin:users_user_delete", args=[self.customer.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
