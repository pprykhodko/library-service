from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from users.roles import CUSTOMER, get_role


User = get_user_model()
PASSWORD = "N7!StrongLibraryPassword"
REGISTER_URL = reverse("users:register")
TOKEN_URL = reverse("users:token-obtain")
PROFILE_URL = reverse("users:manage")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RegistrationTests(APITestCase):
    def test_registration_creates_customer_and_never_returns_password(self):
        response = self.client.post(REGISTER_URL, {
            "email": " Reader@EXAMPLE.COM ", "password": PASSWORD,
            "first_name": "Anna", "last_name": "Reader",
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(pk=response.data["id"])
        self.assertEqual(user.email, "reader@example.com")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.first_name, "Anna")
        self.assertEqual(user.last_name, "Reader")
        self.assertEqual(response.data["role"], CUSTOMER)
        self.assertNotIn("password", response.data)

    def test_registration_cannot_escalate_privileges(self):
        response = self.client.post(REGISTER_URL, {
            "email": "reader@example.com", "password": PASSWORD,
            "role": "admin", "is_staff": True, "is_superuser": True,
            "groups": [1], "user_permissions": [1],
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(pk=response.data["id"])
        self.assertEqual(get_role(user), CUSTOMER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.user_permissions.exists())

    def test_duplicate_email_is_rejected_regardless_of_case(self):
        User.objects.create_user("reader@example.com", PASSWORD)
        for email in (
            "reader@example.com", "READER@EXAMPLE.COM", " Reader@example.com ",
        ):
            with self.subTest(email=email):
                response = self.client.post(REGISTER_URL, {
                    "email": email, "password": PASSWORD,
                }, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("email", response.data)
        self.assertEqual(User.objects.count(), 1)

    def test_invalid_or_missing_credentials_do_not_create_user(self):
        cases = (
            ({"password": PASSWORD}, "email"),
            ({"email": "", "password": PASSWORD}, "email"),
            ({"email": "invalid", "password": PASSWORD}, "email"),
            ({"email": "reader@example.com"}, "password"),
            ({"email": "reader@example.com", "password": ""}, "password"),
        )
        for payload, field in cases:
            with self.subTest(payload=payload):
                response = self.client.post(REGISTER_URL, payload, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        self.assertFalse(User.objects.exists())

    def test_weak_passwords_are_rejected(self):
        for password in ("Ab1!", "123456789012", "password", "reader@example.com"):
            with self.subTest(password=password):
                response = self.client.post(REGISTER_URL, {
                    "email": "reader@example.com", "password": password,
                }, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(User.objects.exists())

    def test_password_whitespace_is_preserved(self):
        password = f" {PASSWORD} "
        response = self.client.post(REGISTER_URL, {
            "email": "reader@example.com", "password": password,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(pk=response.data["id"])
        self.assertTrue(user.check_password(password))
        self.assertFalse(user.check_password(password.strip()))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class JwtTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("reader@example.com", PASSWORD)

    def obtain_tokens(self):
        response = self.client.post(TOKEN_URL, {
            "email": "reader@example.com", "password": PASSWORD,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_login_accepts_normalized_email_and_access_authenticates(self):
        response = self.client.post(TOKEN_URL, {
            "email": " READER@EXAMPLE.COM ", "password": PASSWORD,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("refresh", response.data)
        token = AccessToken(response.data["access"])
        self.assertEqual(str(token["user_id"]), str(self.user.pk))
        self.assertEqual(token["exp"] - token["iat"], 3600)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        profile = self.client.get(PROFILE_URL)
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data["id"], self.user.pk)

    def test_wrong_credentials_and_inactive_user_cannot_login(self):
        for email, password in (
            (self.user.email, "wrong-password"), ("missing@example.com", PASSWORD),
        ):
            with self.subTest(email=email):
                response = self.client.post(TOKEN_URL, {
                    "email": email, "password": password,
                }, format="json")
                self.assertEqual(response.status_code, 401)
        self.user.is_active = False
        self.user.save()
        response = self.client.post(TOKEN_URL, {
            "email": self.user.email, "password": PASSWORD,
        }, format="json")
        self.assertEqual(response.status_code, 401)

    def test_refresh_produces_usable_access_token(self):
        tokens = self.obtain_tokens()
        response = self.client.post(reverse("users:token-refresh"), {
            "refresh": tokens["refresh"],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 200)

    def test_access_token_cannot_be_used_as_refresh(self):
        response = self.client.post(reverse("users:token-refresh"), {
            "refresh": self.obtain_tokens()["access"],
        }, format="json")
        self.assertEqual(response.status_code, 401)

    def test_verify_accepts_valid_token_and_rejects_malformed_token(self):
        access = self.obtain_tokens()["access"]
        for token, expected in ((access, 200), ("not-a-jwt", 401)):
            with self.subTest(expected=expected):
                response = self.client.post(reverse("users:token-verify"), {
                    "token": token,
                }, format="json")
                self.assertEqual(response.status_code, expected)

    def test_missing_invalid_expired_and_refresh_tokens_cannot_access_profile(self):
        expired = AccessToken.for_user(self.user)
        expired.set_exp(from_time=timezone.now() - timedelta(hours=2),
                        lifetime=timedelta(seconds=1))
        refresh = self.obtain_tokens()["refresh"]
        for token in (None, "not-a-jwt", str(expired), refresh):
            with self.subTest(token_type=token and token[:12]):
                self.client.credentials()
                if token:
                    self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
                self.assertEqual(self.client.get(PROFILE_URL).status_code, 401)

    def test_existing_access_token_is_rejected_after_user_is_disabled(self):
        token = self.obtain_tokens()["access"]
        self.user.is_active = False
        self.user.save()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 401)
