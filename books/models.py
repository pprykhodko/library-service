from decimal import Decimal

from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class Book(models.Model):
    class Cover(models.TextChoices):
        HARD = "HARD", _("Hard")
        SOFT = "SOFT", _("Soft")

    title = models.CharField(
        max_length=255,
        validators=[
            RegexValidator(r"\S", _("Title must not be empty"))
        ]
    )
    author = models.CharField(
        max_length=255,
        validators=[
            RegexValidator(r"\S", _("Author must not be empty"))
        ]
    )
    cover = models.CharField(
        max_length=4,
        choices=Cover,
        default=Cover.HARD
    )
    inventory = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    daily_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))]
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(inventory__gte=0),
                name="book_inventory_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(daily_fee__gte=0),
                name="book_daily_fee_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(cover__in=["HARD", "SOFT"]),
                name="book_cover_valid"
            )
        ]

    def clean(self):
        super().clean()

        if isinstance(self.title, str):
            self.title = self.title.strip()

        if isinstance(self.author, str):
            self.author = self.author.strip()

    def __str__(self):
        return f"{self.title} - {self.author}"
