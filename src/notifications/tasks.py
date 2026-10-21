import logging
import uuid
from datetime import timedelta

from celery import Task, shared_task
from django.core.exceptions import ImproperlyConfigured
from django.db.models import F, Q
from django.utils import timezone
from kombu.exceptions import OperationalError

from borrowings.models import Borrowing
from notifications.models import Notification
from notifications.outbox import queue_notification
from notifications.services import TelegramDeliveryError, send_telegram_message

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, ignore_result=True)
def send_telegram_notification(self: Task, notification_id: int | str) -> int | None:
    if isinstance(notification_id, str):
        notification, _ = Notification.objects.get_or_create(
            event_key=f"legacy:{self.request.id or uuid.uuid4()}",
            defaults={"message": notification_id},
        )
        notification_id = notification.pk

    now = timezone.now()
    token = uuid.uuid4()
    claimed = (
        Notification.objects.filter(
            pk=notification_id, sent_at__isnull=True, next_attempt_at__lte=now
        )
        .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
        .update(
            lock_token=token,
            locked_until=now + timedelta(minutes=5),
            attempts=F("attempts") + 1,
        )
    )
    if not claimed:
        return None

    notification = Notification.objects.get(pk=notification_id)
    owned = Notification.objects.filter(pk=notification_id, lock_token=token)
    try:
        message_id = send_telegram_message(notification.message)
    except (TelegramDeliveryError, ImproperlyConfigured, ValueError) as exc:
        retry_delay = 60 if isinstance(exc, TelegramDeliveryError) else 300
        owned.update(
            lock_token=None,
            locked_until=None,
            next_attempt_at=timezone.now() + timedelta(seconds=retry_delay),
            last_error=type(exc).__name__,
        )
        if isinstance(exc, TelegramDeliveryError):
            raise self.retry(exc=exc)
        raise

    owned.update(
        sent_at=timezone.now(),
        telegram_message_id=message_id,
        lock_token=None,
        locked_until=None,
        last_error="",
    )
    return message_id


@shared_task(ignore_result=True)
def retry_pending_notifications() -> dict[str, int]:
    now = timezone.now()
    pending = (
        Notification.objects.filter(sent_at__isnull=True, next_attempt_at__lte=now)
        .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
        .values_list("pk", flat=True)[:100]
    )
    queued = failed = 0
    for notification_id in list(pending):
        try:
            send_telegram_notification.delay(notification_id)
        except OperationalError:
            failed += 1
            logger.error("Could not enqueue notification %s", notification_id)
        else:
            queued += 1
    return {"queued": queued, "failed": failed}


@shared_task(ignore_result=True)
def notify_overdue_borrowings() -> int:
    today = timezone.localdate()
    borrowings = Borrowing.objects.filter(
        actual_return_date__isnull=True,
        expected_return_date__lt=today,
    ).select_related("book", "user")
    processed = 0

    for borrowing in borrowings.iterator(chunk_size=100):
        overdue_days = (today - borrowing.expected_return_date).days
        message = (
            f"Overdue borrowing\n"
            f"Borrowing #{borrowing.pk}\n"
            f"Book: {borrowing.book.title}\n"
            f"Reader: {borrowing.user.email}\n"
            f"Expected return date: {borrowing.expected_return_date.isoformat()}\n"
            f"Overdue days: {overdue_days}"
        )
        queue_notification(
            f"borrowing:{borrowing.pk}:overdue:{today.isoformat()}", message
        )
        processed += 1

    return processed
