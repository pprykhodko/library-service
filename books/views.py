from django.db.models.deletion import ProtectedError
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import serializers, viewsets

from books.models import Book
from books.pagination import BookPagination
from books.permissions import CanManageBooks
from books.serializers import BookFilterSerializer, BookSerializer
from users.roles import ADMIN, EMPLOYEE, get_role


class ProtectBookDeletionMixin:
    def perform_destroy(self, instance):
        try:
            instance.delete()

        except ProtectedError:
            raise serializers.ValidationError({
                "detail": (
                    "This book has protected related records and cannot be "
                    "deleted. Set is_active=false to withdraw it from lending."
                )
            })


@extend_schema_view(
    list=extend_schema(
        summary="List books",
        description=(
            "Authenticated users may read the catalog. Customers see active "
            "books, including books with zero inventory. Employees and "
            "administrators may also see inactive books."
        ),
        parameters=[BookFilterSerializer]
    ),
    retrieve=extend_schema(summary="Read a book"),
    create=extend_schema(
        summary="Add a book",
        description="Employees and administrators only"
    ),
    update=extend_schema(
        summary="Update a book",
        description="Employees and administrators only"
    ),
    partial_update=extend_schema(
        summary="Partially update a book",
        description=(
            "Employees and administrators only. Set is_active=false to "
            "withdraw a book from lending without deleting it."
        )
    ),
    destroy=extend_schema(
        summary="Delete a book",
        description=(
            "Employees and administrators only. Protected related records "
            "prevent deletion; withdraw the book with is_active=false instead."
        )
    )
)
class BookViewSet(ProtectBookDeletionMixin, viewsets.ModelViewSet):
    queryset = Book.objects.all().order_by("title", "id")
    serializer_class = BookSerializer
    permission_classes = (CanManageBooks,)
    pagination_class = BookPagination

    def get_queryset(self):
        queryset = super().get_queryset()

        if getattr(self, "swagger_fake_view", False):
            return queryset.none()

        if not self.request.user.is_authenticated:
            return queryset.none()

        if get_role(self.request.user) not in (EMPLOYEE, ADMIN):
            queryset = queryset.filter(is_active=True)

        if self.action != "list":
            return queryset

        filters = BookFilterSerializer(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        params = filters.validated_data

        if "title" in params:
            queryset = queryset.filter(title__icontains=params["title"])

        if "author" in params:
            queryset = queryset.filter(author__icontains=params["author"])

        if "cover" in params:
            queryset = queryset.filter(cover=params["cover"])

        if "is_active" in params:
            queryset = queryset.filter(is_active=params["is_active"])

        if "available" in params:
            if params["available"]:
                queryset = queryset.filter(is_active=True, inventory__gt=0)
            else:
                queryset = queryset.exclude(is_active=True, inventory__gt=0)

        return queryset
