import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification


class NotificationModelTests(TestCase):
    def test_new_notification_is_pending_and_ready_for_delivery(self):
        notification = Notification.objects.create(event_key="event", message="Hello")
        self.assertIsNone(notification.sent_at)
        self.assertIsNone(notification.telegram_message_id)
        self.assertIsNone(notification.locked_until)
        self.assertIsNone(notification.lock_token)
        self.assertEqual(notification.attempts, 0)
        self.assertEqual(notification.last_error, "")
        self.assertLessEqual(notification.next_attempt_at, timezone.now())
        self.assertEqual(str(notification), "event")

    def test_duplicate_event_key_is_rejected_by_database(self):
        Notification.objects.create(event_key="event", message="Hello")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(event_key="event", message="Duplicate")

    def test_delivery_time_and_telegram_id_must_be_set_together(self):
        notification = Notification.objects.create(event_key="event", message="Hello")
        for changes in ({"sent_at": timezone.now()}, {"telegram_message_id": 42}):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Notification.objects.filter(pk=notification.pk).update(**changes)
        Notification.objects.filter(pk=notification.pk).update(
            sent_at=timezone.now(), telegram_message_id=42
        )
        notification.refresh_from_db()
        self.assertEqual(notification.telegram_message_id, 42)

    def test_lock_token_and_expiration_must_be_set_together(self):
        notification = Notification.objects.create(event_key="event", message="Hello")
        for changes in ({"lock_token": uuid.uuid4()}, {"locked_until": timezone.now()}):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Notification.objects.filter(pk=notification.pk).update(**changes)
        Notification.objects.filter(pk=notification.pk).update(
            lock_token=uuid.uuid4(), locked_until=timezone.now() + timedelta(minutes=5)
        )
        notification.refresh_from_db()
        self.assertIsNotNone(notification.lock_token)
