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
            "title": {
                "trim_whitespace": True,
                "allow_blank": False
            },
            "author": {
                "trim_whitespace": True,
                "allow_blank": False
            }
        }
