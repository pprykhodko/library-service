from django.db import transaction
from django.db.models import F
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from books.models import Book
from borrowings.models import Borrowing
from borrowings.pagination import BorrowingPagination
from borrowings.permissions import CanAccessBorrowings
from borrowings.serializers import (
    BorrowingDetailSerializer,
    BorrowingFilterSerializer,
    BorrowingListSerializer,
    BorrowingReturnSerializer,
    BorrowingSerializer,
)
from users.roles import ADMIN, EMPLOYEE, get_role
from payments.services import create_return_fine, lock_borrower


@extend_schema(tags=["borrowings"])
@extend_schema_view(
    list=extend_schema(
        summary="List borrowings",
        description="Customers see their own records; employees and admins see all.",
        parameters=[BorrowingFilterSerializer],
    ),
    retrieve=extend_schema(summary="Read a borrowing"),
    create=extend_schema(
        summary="Borrow a book",
        description=(
                "Customers and administrators may borrow for themselves. "
                "The server assigns the user and borrowing date and reduces inventory."
                " Outstanding payments or fines prevent new borrowings."
        )
    )
)
class BorrowingViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet
):
    queryset = (
        Borrowing.objects.select_related("book", "user").prefetch_related("payments")
    )
    serializer_class = BorrowingSerializer
    permission_classes = (CanAccessBorrowings,)
    pagination_class = BorrowingPagination

    def get_queryset(self):
        queryset = super().get_queryset()

        if getattr(self, "swagger_fake_view", False):
            return queryset.none()

        if not self.request.user.is_authenticated:
            return queryset.none()

        if get_role(self.request.user) not in (EMPLOYEE, ADMIN):
            queryset = queryset.filter(user=self.request.user)

        if self.action != "list":
            return queryset

        filters = BorrowingFilterSerializer(data=self.request.query_params.dict())
        filters.is_valid(raise_exception=True)
        params = filters.validated_data

        if "user_id" in params:
            queryset = queryset.filter(user_id=params["user_id"])

        if "is_active" in params:
            queryset = queryset.filter(actual_return_date__isnull=params["is_active"])

        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return BorrowingListSerializer

        if self.action == "retrieve":
            return BorrowingDetailSerializer

        if self.action == "return_book":
            return BorrowingReturnSerializer

        return BorrowingSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @extend_schema(
        summary="Confirm a book return",
        description=(
                "Employees and administrators only. Sets today's return date and "
                "restores one copy. Repeated returns are rejected. No body is required."
                " An overdue return creates one fine: overdue days times the saved "
                "daily fee times 2, with a minimum nonzero fine of 0.50 USD."
        ),
        request=BorrowingReturnSerializer,
        responses={200: BorrowingDetailSerializer}
    )
    @action(detail=True, methods=["post"], url_path="return")
    def return_book(self, request, pk=None):
        borrowing = self.get_object()
        today = timezone.localdate()

        with transaction.atomic():
            lock_borrower(borrowing.user_id)
            borrowing.refresh_from_db()
            updated = Borrowing.objects.filter(
                pk=borrowing.pk, actual_return_date__isnull=True,
                borrow_date__lte=today
            ).update(actual_return_date=today)

            if not updated:
                raise serializers.ValidationError(
                    {
                        "detail":
                            "This borrowing is already returned or starts in the future"
                    }
                )
            create_return_fine(borrowing, today)
            Book.objects.filter(pk=borrowing.book_id).update(
                inventory=F("inventory") + 1
            )
        borrowing = self.get_queryset().get(pk=borrowing.pk)

        return Response(BorrowingDetailSerializer(
            borrowing, context=self.get_serializer_context()
        ).data)
