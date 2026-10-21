from django.db import models
from django.utils import timezone


class Notification(models.Model):
    event_key = models.CharField(max_length=255, unique=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lock_token = models.UUIDField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ("next_attempt_at", "pk")
        indexes = [models.Index(fields=["sent_at", "next_attempt_at"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(sent_at__isnull=True, telegram_message_id__isnull=True)
                    | models.Q(sent_at__isnull=False, telegram_message_id__isnull=False)
                ),
                name="notification_delivery_fields_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(lock_token__isnull=True, locked_until__isnull=True)
                    | models.Q(lock_token__isnull=False, locked_until__isnull=False)
                ),
                name="notification_lease_fields_consistent",
            ),
        ]

    def __str__(self):
        return self.event_key
