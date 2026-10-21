from datetime import timedelta
from unittest.mock import patch
import uuid

from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from kombu.exceptions import OperationalError

from notifications.models import Notification
from notifications.outbox import queue_notification
from notifications.services import TelegramDeliveryError
from notifications.tasks import retry_pending_notifications, send_telegram_notification


class NotificationRecoveryTests(TestCase):
    def setUp(self):
        enqueue = patch.object(send_telegram_notification, "delay")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)
        delivery = patch("notifications.tasks.send_telegram_message", return_value=42)
        self.deliver = delivery.start()
        self.addCleanup(delivery.stop)
        guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Tests must not contact Telegram"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    def test_broker_failure_preserves_message_and_recovery_enqueues_it(self):
        self.enqueue.side_effect = OperationalError("Redis unavailable")
        with self.assertLogs(level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                notification = queue_notification(
                    "borrowing:1:created", "New borrowing"
                )
                self.enqueue.assert_not_called()
        notification.refresh_from_db()
        self.assertEqual(notification.message, "New borrowing")
        self.assertIsNone(notification.sent_at)
        self.enqueue.side_effect = None
        self.enqueue.reset_mock()

        self.assertEqual(retry_pending_notifications.run(), {"queued": 1, "failed": 0})
        self.enqueue.assert_called_once_with(notification.pk)
        self.assertEqual(send_telegram_notification.run(notification.pk), 42)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(notification.telegram_message_id, 42)
        self.assertEqual(retry_pending_notifications.run(), {"queued": 0, "failed": 0})

    def test_rollback_discards_message_and_enqueue_callback(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                queue_notification("payment:1:paid", "Paid")
                transaction.set_rollback(True)
        self.assertFalse(Notification.objects.exists())
        self.enqueue.assert_not_called()

    def test_same_event_creates_and_enqueues_only_one_snapshot(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = queue_notification("payment:1:paid", "Original message")
            second = queue_notification("payment:1:paid", "Changed message")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Notification.objects.get().message, "Original message")
        self.enqueue.assert_called_once_with(first.pk)

    def test_duplicate_delivery_does_not_send_again(self):
        notification = Notification.objects.create(
            event_key="duplicate", message="Hello"
        )
        send_telegram_notification.run(notification.pk)
        send_telegram_notification.run(notification.pk)
        self.deliver.assert_called_once_with("Hello")
        notification.refresh_from_db()
        self.assertEqual(notification.attempts, 1)

    def test_missing_notification_is_ignored(self):
        self.assertIsNone(send_telegram_notification.run(999999))
        self.deliver.assert_not_called()

    def test_success_of_stale_worker_does_not_overwrite_new_worker_lease(self):
        notification = Notification.objects.create(event_key="stale", message="Hello")
        newer_token = uuid.uuid4()

        def deliver(_text):
            Notification.objects.filter(pk=notification.pk).update(
                lock_token=newer_token
            )
            return 42

        self.deliver.side_effect = deliver
        send_telegram_notification.run(notification.pk)
        notification.refresh_from_db()
        self.assertEqual(notification.lock_token, newer_token)
        self.assertIsNone(notification.sent_at)

    def test_unexpected_worker_failure_is_recoverable_after_lease_expiration(self):
        notification = Notification.objects.create(event_key="crash", message="Hello")
        self.deliver.side_effect = RuntimeError("Unexpected failure")
        with self.assertRaises(RuntimeError):
            send_telegram_notification.run(notification.pk)
        notification.refresh_from_db()
        self.assertIsNone(notification.sent_at)
        self.assertIsNotNone(notification.locked_until)
        self.deliver.side_effect = None
        with patch(
            "notifications.tasks.timezone.now", return_value=notification.locked_until
        ):
            self.assertEqual(send_telegram_notification.run(notification.pk), 42)

    def test_active_lease_blocks_second_worker_and_expired_lease_is_recovered(self):
        notification = Notification.objects.create(
            event_key="lease",
            message="Hello",
            lock_token=uuid.uuid4(),
            locked_until=timezone.now() + timedelta(minutes=5),
        )
        self.assertIsNone(send_telegram_notification.run(notification.pk))
        self.deliver.assert_not_called()
        self.assertEqual(retry_pending_notifications.run(), {"queued": 0, "failed": 0})
        Notification.objects.filter(pk=notification.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(retry_pending_notifications.run(), {"queued": 1, "failed": 0})
        self.assertEqual(send_telegram_notification.run(notification.pk), 42)
        notification.refresh_from_db()
        self.assertIsNone(notification.lock_token)
        self.assertIsNone(notification.locked_until)

    def test_reentrant_delivery_is_blocked_while_first_worker_sends(self):
        notification = Notification.objects.create(event_key="workers", message="Hello")

        def deliver(_text):
            self.assertIsNone(send_telegram_notification.run(notification.pk))
            return 42

        self.deliver.side_effect = deliver
        self.assertEqual(send_telegram_notification.run(notification.pk), 42)
        self.deliver.assert_called_once()

    def test_exhausted_celery_retries_leave_pending_message_for_later_recovery(self):
        notification = Notification.objects.create(event_key="failure", message="Hello")
        self.deliver.side_effect = TelegramDeliveryError("Unavailable")
        send_telegram_notification.push_request(
            id="test",
            args=(notification.pk,),
            kwargs={},
            retries=3,
            called_directly=False,
            is_eager=True,
        )
        try:
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_notification.run(notification.pk)
        finally:
            send_telegram_notification.pop_request()
        notification.refresh_from_db()
        self.assertIsNone(notification.sent_at)
        self.assertIsNone(notification.lock_token)
        self.assertGreater(notification.next_attempt_at, timezone.now())
        self.assertEqual(notification.last_error, "TelegramDeliveryError")
        self.assertEqual(retry_pending_notifications.run(), {"queued": 0, "failed": 0})
        self.deliver.side_effect = None
        with patch(
            "notifications.tasks.timezone.now",
            return_value=notification.next_attempt_at,
        ):
            self.assertEqual(
                retry_pending_notifications.run(), {"queued": 1, "failed": 0}
            )
            self.assertEqual(send_telegram_notification.run(notification.pk), 42)

    def test_dispatch_failure_does_not_stop_other_notifications(self):
        first = Notification.objects.create(event_key="first", message="First")
        second = Notification.objects.create(event_key="second", message="Second")
        self.enqueue.side_effect = [OperationalError("Offline"), None]
        with self.assertLogs("notifications.tasks", level="ERROR"):
            self.assertEqual(
                retry_pending_notifications.run(), {"queued": 1, "failed": 1}
            )
        self.assertEqual(
            [call.args[0] for call in self.enqueue.call_args_list],
            [first.pk, second.pk],
        )
        self.assertFalse(Notification.objects.filter(sent_at__isnull=False).exists())

    def test_legacy_text_task_is_persisted_and_delivered(self):
        send_telegram_notification.push_request(id="legacy-task")
        try:
            self.assertEqual(send_telegram_notification.run("Legacy message"), 42)
            send_telegram_notification.run("Legacy message")
        finally:
            send_telegram_notification.pop_request()
        self.deliver.assert_called_once_with("Legacy message")
        self.assertEqual(Notification.objects.count(), 1)
