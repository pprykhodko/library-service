from celery import Task, shared_task
from django.utils import timezone

from borrowings.models import Borrowing
from notifications.services import TelegramDeliveryError, send_telegram_message


@shared_task(bind=True, max_retries=3, default_retry_delay=60, ignore_result=True)
def send_telegram_notification(self: Task, text: str) -> int:
    """Deliver a message, retrying delivery failures up to three times."""
    try:
        return send_telegram_message(text)
    except TelegramDeliveryError as exc:
        raise self.retry(exc=exc)


@shared_task(ignore_result=True)
def notify_overdue_borrowings() -> int:
    """Queue one reminder per overdue borrowing and return the queued count."""
    today = timezone.localdate()
    borrowings = Borrowing.objects.filter(
        actual_return_date__isnull=True,
        expected_return_date__lt=today,
    ).select_related("book", "user")
    queued = 0

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
        send_telegram_notification.delay(message)
        queued += 1

    return queued
