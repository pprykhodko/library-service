from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Borrowing(models.Model):
    borrow_date = models.DateField(default=timezone.localdate, editable=False)
    expected_return_date = models.DateField()
    actual_return_date = models.DateField(null=True, blank=True, default=None)
    book = models.ForeignKey(
        "books.Book",
        on_delete=models.PROTECT,
        related_name="borrowings"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="borrowings"
    )

    class Meta:
        ordering = ("-borrow_date", "-id")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expected_return_date__gte=models.F("borrow_date")),
                name="borrowing_expected_not_before_borrow"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(actual_return_date__isnull=True)
                    | models.Q(actual_return_date__gte=models.F("borrow_date"))
                ),
                name="borrowing_actual_not_before_borrow"
            )
        ]

    def clean(self):
        super().clean()
        errors = {}

        if self.borrow_date and self.expected_return_date:
            if self.expected_return_date < self.borrow_date:
                errors["expected_return_date"] = "Return date cannot precede borrowing"

        if self.borrow_date and self.actual_return_date:
            if self.actual_return_date < self.borrow_date:
                errors["actual_return_date"] = "Return date cannot precede borrowing"

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Borrowing {self.pk}: book {self.book_id}, user {self.user_id}"
