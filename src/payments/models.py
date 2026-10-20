import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PAID = "PAID", "Paid"
        EXPIRED = "EXPIRED", "Expired"

    class Type(models.TextChoices):
        PAYMENT = "PAYMENT", "Payment"
        FINE = "FINE", "Fine"

    borrowing = models.ForeignKey(
        "borrowings.Borrowing", on_delete=models.PROTECT, related_name="payments"
    )
    status = models.CharField(max_length=7, choices=Status, default=Status.PENDING)
    type = models.CharField(max_length=7, choices=Type, default=Type.PAYMENT)
    session_url = models.URLField(max_length=2048, blank=True, default="")
    session_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    checkout_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    money_to_pay = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        ordering = ("-id",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(money_to_pay__gte=0),
                name="payment_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["PENDING", "PAID", "EXPIRED"]),
                name="payment_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(type__in=["PAYMENT", "FINE"]),
                name="payment_type_valid",
            ),
            models.UniqueConstraint(
                fields=["borrowing"],
                condition=models.Q(type="PAYMENT"),
                name="one_rental_payment_per_borrowing",
            ),
            models.UniqueConstraint(
                fields=["borrowing"],
                condition=models.Q(type="FINE"),
                name="one_fine_per_borrowing",
            ),
        ]

    def __str__(self):
        return f"Payment {self.pk}: {self.money_to_pay} USD ({self.status})"


class ExpiredCheckoutSession(models.Model):
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="expired_sessions"
    )
    session_id = models.CharField(max_length=255, unique=True)
    checkout_key = models.UUIDField(unique=True)
