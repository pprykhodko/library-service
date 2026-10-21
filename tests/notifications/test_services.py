import json
from unittest.mock import patch

import requests
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from notifications.services import TelegramDeliveryError, send_telegram_message


@override_settings(
    TELEGRAM_BOT_TOKEN="123456:test-token",
    TELEGRAM_CHAT_ID="-1001234567890",
)
class TelegramDeliveryTests(SimpleTestCase):
    def setUp(self) -> None:
        patcher = patch("requests.sessions.Session.request", autospec=True)
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = self.response(
            {"ok": True, "result": {"message_id": 42}}
        )

    @staticmethod
    def response(payload: object, status_code: int = 200) -> requests.Response:
        response = requests.Response()
        response.status_code = status_code
        response.encoding = "utf-8"
        response._content = json.dumps(payload).encode("utf-8")
        return response

    def test_sends_plain_text_to_configured_chat_and_returns_message_id(self):
        text = 'New borrowing #12: "War & Peace" <volume 1>\nReader: Анна'

        message_id = send_telegram_message(text)

        self.assertEqual(message_id, 42)
        self.request.assert_called_once()
        kwargs = self.request.call_args.kwargs
        self.assertEqual(kwargs["method"].upper(), "POST")
        self.assertEqual(
            kwargs["url"],
            "https://api.telegram.org/bot123456:test-token/sendMessage",
        )
        self.assertEqual(
            kwargs["json"],
            {"chat_id": "-1001234567890", "text": text},
        )
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 30)

    def test_missing_credentials_raise_configuration_error_without_request(self):
        for setting in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            for value in ("", "   "):
                with self.subTest(setting=setting, value=value):
                    with override_settings(**{setting: value}):
                        with self.assertRaises(ImproperlyConfigured):
                            send_telegram_message("New borrowing #12")
                    self.request.assert_not_called()

    def test_empty_message_is_rejected_without_request(self):
        for text in ("", "   ", "\n\t"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    send_telegram_message(text)
                self.request.assert_not_called()

    def test_connection_error_and_timeout_raise_delivery_error(self):
        for error in (requests.ConnectionError("Offline"), requests.Timeout()):
            with self.subTest(error=type(error).__name__):
                self.request.reset_mock()
                self.request.side_effect = error
                with self.assertRaises(TelegramDeliveryError):
                    send_telegram_message("New borrowing #12")
                # Retries belong to the Celery task, not this HTTP service.
                self.request.assert_called_once()

    def test_http_error_is_not_reported_as_success(self):
        for status_code in (400, 401, 403, 429, 500, 503):
            with self.subTest(status_code=status_code):
                self.request.return_value = self.response(
                    {"ok": False, "description": "Request failed"}, status_code
                )
                with self.assertRaises(TelegramDeliveryError):
                    send_telegram_message("New borrowing #12")

    def test_telegram_error_with_http_200_raises_delivery_error(self):
        self.request.return_value = self.response(
            {"ok": False, "description": "Bad Request: chat not found"}
        )

        with self.assertRaises(TelegramDeliveryError):
            send_telegram_message("New borrowing #12")

    def test_non_json_response_raises_delivery_error(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b"<html>Bad gateway</html>"
        self.request.return_value = response

        with self.assertRaises(TelegramDeliveryError):
            send_telegram_message("New borrowing #12")

    def test_incomplete_success_response_raises_delivery_error(self):
        for payload in ({}, {"ok": True}, {"ok": True, "result": {}}, []):
            with self.subTest(payload=payload):
                self.request.return_value = self.response(payload)
                with self.assertRaises(TelegramDeliveryError):
                    send_telegram_message("New borrowing #12")
