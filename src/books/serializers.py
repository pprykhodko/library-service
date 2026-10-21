from rest_framework import serializers

from books.models import Book


class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = (
            "id",
            "title",
            "author",
            "cover",
            "inventory",
            "daily_fee",
            "is_active",
        )
        read_only_fields = ("id",)
        extra_kwargs = {
            "title": {"trim_whitespace": True, "allow_blank": False},
            "author": {"trim_whitespace": True, "allow_blank": False},
        }


class BookFilterSerializer(serializers.Serializer):
    title = serializers.CharField(
        required=False, help_text="Case-insensitive partial title match"
    )
    author = serializers.CharField(
        required=False, help_text="Case-insensitive partial author match"
    )
    cover = serializers.ChoiceField(choices=Book.Cover.choices, required=False)
    is_active = serializers.BooleanField(
        required=False,
        help_text="Filter catalog status. Customers can only see active books.",
    )
    available = serializers.BooleanField(
        required=False,
        help_text="Available means active with inventory greater than zero.",
    )
