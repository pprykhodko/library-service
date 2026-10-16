from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import serializers

from books.models import Book
from books.serializers import BookSerializer
from borrowings.models import Borrowing
from users.serializers import CustomerSerializer
from payments.serializers import PaymentSerializer
from payments.models import Payment
from payments.services import create_rental_payment, lock_borrower


class BorrowingSerializer(serializers.ModelSerializer):
    payments = PaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Borrowing
        fields = (
            "id",
            "book",
            "user",
            "borrow_date",
            "expected_return_date",
            "actual_return_date",
            "daily_fee",
            "payments",
        )
        read_only_fields = (
            "id", "user", "borrow_date", "actual_return_date", "daily_fee", "payments",
        )

    def validate_expected_return_date(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError("Return date cannot be in the past")

        return value

    def validate_book(self, book):
        if not book.is_active:
            raise serializers.ValidationError("This book is not available for lending")

        if book.inventory == 0:
            raise serializers.ValidationError("No copies of this book are available")

        return book

    def create(self, validated_data):
        today = timezone.localdate()

        if validated_data["expected_return_date"] < today:
            raise serializers.ValidationError({
                "expected_return_date": "Return date cannot be in the past"
            })

        with transaction.atomic():
            lock_borrower(validated_data["user"].pk)

            if Payment.objects.filter(
                borrowing__user=validated_data["user"],
                status__in=[Payment.Status.PENDING, Payment.Status.EXPIRED]
            ).exists():
                raise serializers.ValidationError({
                    "detail": "Pay all outstanding rental payments and fines "
                              "before borrowing another book."
                })

            updated = Book.objects.filter(
                pk=validated_data["book"].pk,
                is_active=True,
                inventory__gt=0
            ).update(inventory=F("inventory") - 1)

            if not updated:
                raise serializers.ValidationError({
                    "book": "This book is inactive or has no available copies"
                })

            book = Book.objects.get(pk=validated_data["book"].pk)
            borrowing = Borrowing.objects.create(
                borrow_date=today, daily_fee=book.daily_fee, **validated_data
            )
            create_rental_payment(borrowing)

            return borrowing


class BorrowingListSerializer(BorrowingSerializer):
    book = serializers.SlugRelatedField(read_only=True, slug_field="title")
    user = serializers.SlugRelatedField(read_only=True, slug_field="email")


class BorrowingDetailSerializer(BorrowingSerializer):
    book = BookSerializer(read_only=True)
    user = CustomerSerializer(read_only=True)


class BorrowingFilterSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Filter by user ID. Customers can only see their own records."
    )
    is_active = serializers.BooleanField(
        required=False,
        help_text="True for books not yet returned"
    )


class BorrowingReturnSerializer(serializers.Serializer):
    """The server determines the return date; no request fields are required."""
