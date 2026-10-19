from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from users.roles import ADMIN, CUSTOMER, EMPLOYEE, assign_role, get_role


User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UserModelTests(TestCase):
    def test_create_user_normalizes_email_hashes_password_and_assigns_customer(self):
        user = User.objects.create_user("  Reader@EXAMPLE.COM  ", "Strong!Pass2026")

        user.refresh_from_db()
        self.assertEqual(user.email, "reader@example.com")
        self.assertTrue(user.check_password("Strong!Pass2026"))
        self.assertNotEqual(user.password, "Strong!Pass2026")
        self.assertEqual(get_role(user), CUSTOMER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_create_user_requires_email(self):
        for email in (None, ""):
            with self.subTest(email=email), self.assertRaises(ValueError):
                User.objects.create_user(email, "Strong!Pass2026")

    def test_user_without_password_has_unusable_password(self):
        user = User.objects.create_user("reader@example.com")
        self.assertFalse(user.has_usable_password())

    def test_save_and_clean_normalize_email(self):
        user = User.objects.create_user("reader@example.com")
        user.email = "  Changed@EXAMPLE.COM "
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.email, "changed@example.com")
        user.email = " OTHER@EXAMPLE.COM "
        user.clean()
        self.assertEqual(user.email, "other@example.com")

    def test_duplicate_normalized_email_is_rejected_by_database(self):
        User.objects.create_user("reader@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("READER@EXAMPLE.COM")
        self.assertEqual(User.objects.count(), 1)

    def test_natural_key_lookup_is_case_insensitive(self):
        user = User.objects.create_user("reader@example.com")
        self.assertEqual(User.objects.get_by_natural_key(" READER@Example.COM "), user)

    def test_create_superuser(self):
        user = User.objects.create_superuser("ADMIN@EXAMPLE.COM", "Strong!Pass2026")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertEqual(get_role(user), ADMIN)
        self.assertTrue(user.check_password("Strong!Pass2026"))
        self.assertFalse(user.groups.filter(name="Customers").exists())

    def test_superuser_requires_both_privilege_flags(self):
        for flags in ({"is_staff": False}, {"is_superuser": False}):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                User.objects.create_superuser("admin@example.com", "Password!", **flags)
        self.assertFalse(User.objects.exists())


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UserRoleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("reader@example.com")

    def test_role_transitions_replace_role_groups_and_preserve_other_groups(self):
        unrelated = Group.objects.create(name="Book club")
        self.user.groups.add(unrelated)
        for role, expected_groups in (
            (EMPLOYEE, {"Employees", "Book club"}),
            (ADMIN, {"Book club"}),
            (CUSTOMER, {"Customers", "Book club"}),
        ):
            with self.subTest(role=role):
                assign_role(self.user, role)
                self.user.refresh_from_db()
                self.assertEqual(get_role(self.user), role)
                self.assertEqual(self.user.is_staff, role == ADMIN)
                self.assertEqual(self.user.is_superuser, role == ADMIN)
                self.assertSetEqual(
                    set(self.user.groups.values_list("name", flat=True)),
                    expected_groups,
                )

    def test_assigning_same_role_twice_is_idempotent(self):
        assign_role(self.user, EMPLOYEE)
        assign_role(self.user, EMPLOYEE)
        self.assertEqual(list(self.user.groups.values_list("name", flat=True)),
                         ["Employees"])

    def test_invalid_role_does_not_change_user(self):
        with self.assertRaises(ValueError):
            assign_role(self.user, "owner")
        self.user.refresh_from_db()
        self.assertEqual(get_role(self.user), CUSTOMER)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

    def test_employee_takes_precedence_if_both_groups_are_present(self):
        group, _ = Group.objects.get_or_create(name="Employees")
        self.user.groups.add(group)
        self.assertEqual(get_role(self.user), EMPLOYEE)

    def test_superuser_takes_precedence_over_groups(self):
        self.user.is_superuser = True
        self.user.save()
        self.assertEqual(get_role(self.user), ADMIN)

    def test_user_without_role_groups_has_no_role(self):
        self.user.groups.clear()
        self.assertIsNone(get_role(self.user))
