from unittest.mock import patch

from celery.exceptions import Retry
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification

from notifications.services import TelegramDeliveryError
from notifications.tasks import send_telegram_notification


class TelegramNotificationTaskTests(TestCase):
    text = "New borrowing #12: The Hobbit"

    def setUp(self) -> None:
        self.notification = Notification.objects.create(
            event_key="test", message=self.text
        )
        delivery = patch("notifications.tasks.send_telegram_message")
        self.send_message = delivery.start()
        self.addCleanup(delivery.stop)
        self.send_message.return_value = 42

        network_guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("These tests must not contact Telegram"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

        broker_guard = patch.object(
            send_telegram_notification,
            "apply_async",
            side_effect=AssertionError("These tests must not contact the broker"),
        )
        broker_guard.start()
        self.addCleanup(broker_guard.stop)

    def run_task(self, retries: int = 0):
        send_telegram_notification.push_request(
            id="notification-test",
            args=(self.notification.pk,),
            kwargs={},
            retries=retries,
            called_directly=False,
            is_eager=True,
        )
        try:
            self.notification.refresh_from_db()
            attempt_time = max(timezone.now(), self.notification.next_attempt_at)
            with patch("notifications.tasks.timezone.now", return_value=attempt_time):
                return send_telegram_notification.run(self.notification.pk)
        finally:
            send_telegram_notification.pop_request()

    def test_successful_delivery_returns_message_id(self):
        self.assertEqual(self.run_task(), 42)
        self.send_message.assert_called_once_with(self.text)

    def test_delivery_error_requests_retry_with_original_message(self):
        error = TelegramDeliveryError("Telegram is unavailable")
        self.send_message.side_effect = error

        with self.assertRaises(Retry) as raised:
            self.run_task()

        self.send_message.assert_called_once_with(self.text)
        self.assertIs(raised.exception.exc, error)
        self.assertEqual(raised.exception.when, 60)
        self.assertIsNotNone(raised.exception.sig)
        self.assertEqual(raised.exception.sig.args, (self.notification.pk,))

    def test_all_three_retries_are_available(self):
        self.assertEqual(send_telegram_notification.max_retries, 3)
        self.send_message.side_effect = TelegramDeliveryError("Temporary failure")

        for retries in range(3):
            with self.subTest(retries=retries):
                self.send_message.reset_mock()
                with self.assertRaises(Retry):
                    self.run_task(retries=retries)
                self.send_message.assert_called_once_with(self.text)

    def test_exhausted_retries_raise_delivery_error(self):
        error = TelegramDeliveryError("Telegram is still unavailable")
        self.send_message.side_effect = error

        with self.assertRaises(TelegramDeliveryError) as raised:
            self.run_task(retries=3)

        self.assertIs(raised.exception, error)
        self.send_message.assert_called_once_with(self.text)

    def test_next_attempt_can_succeed_after_delivery_error(self):
        self.send_message.side_effect = [
            TelegramDeliveryError("Temporary failure"),
            42,
        ]
        with self.assertRaises(Retry):
            self.run_task()

        self.assertEqual(self.run_task(retries=1), 42)
        self.assertEqual(self.send_message.call_count, 2)

    def test_configuration_error_is_not_retried(self):
        error = ImproperlyConfigured("TELEGRAM_BOT_TOKEN is missing")
        self.send_message.side_effect = error

        with self.assertRaises(ImproperlyConfigured) as raised:
            self.run_task()

        self.assertIs(raised.exception, error)
        self.send_message.assert_called_once_with(self.text)

    def test_invalid_message_is_not_retried(self):
        error = ValueError("Message must not be empty")
        self.send_message.side_effect = error

        with self.assertRaises(ValueError) as raised:
            self.run_task()

        self.assertIs(raised.exception, error)
        self.send_message.assert_called_once_with(self.text)
