from django.db import transaction

from notifications.models import Notification


def queue_notification(event_key: str, message: str) -> Notification:
    from notifications.tasks import send_telegram_notification

    with transaction.atomic():
        notification, created = Notification.objects.get_or_create(
            event_key=event_key, defaults={"message": message}
        )
        if created:
            transaction.on_commit(
                lambda: send_telegram_notification.delay(notification.pk), robust=True
            )
    return notification
