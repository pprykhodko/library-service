from celery import Task, shared_task

from notifications.services import TelegramDeliveryError, send_telegram_message


@shared_task(bind=True, max_retries=3, default_retry_delay=60, ignore_result=True)
def send_telegram_notification(self: Task, text: str) -> int:
    """Deliver a message, retrying delivery failures up to three times."""
    try:
        return send_telegram_message(text)
    except TelegramDeliveryError as exc:
        raise self.retry(exc=exc)
