from decimal import Decimal

from django.http import QueryDict
from django.test import SimpleTestCase, TestCase

from books.models import Book
from books.serializers import BookFilterSerializer, BookSerializer


class BookSerializerTests(TestCase):
    def setUp(self) -> None:
        self.payload: dict[str, object] = {
            "title": "The Hobbit",
            "author": "J. R. R. Tolkien",
            "cover": "HARD",
            "inventory": 3,
            "daily_fee": "1.50",
            "is_active": True,
        }

    def test_create_trims_text_and_converts_numeric_values(self):
        self.payload.update(
            {
                "title": "  The Hobbit \t",
                "author": "\n J. R. R. Tolkien  ",
                "inventory": "3",
            }
        )
        serializer = BookSerializer(data=self.payload)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["inventory"], 3)
        self.assertEqual(serializer.validated_data["daily_fee"], Decimal("1.50"))
        self.assertFalse(Book.objects.exists())

        book = serializer.save()
        book.refresh_from_db()
        self.assertEqual(book.title, "The Hobbit")
        self.assertEqual(book.author, "J. R. R. Tolkien")
        self.assertEqual(book.inventory, 3)
        self.assertEqual(book.daily_fee, Decimal("1.50"))

    def test_create_uses_model_defaults_for_optional_fields(self):
        del self.payload["cover"]
        del self.payload["is_active"]
        serializer = BookSerializer(data=self.payload)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        book = serializer.save()
        self.assertEqual(book.cover, Book.Cover.HARD)
        self.assertTrue(book.is_active)

    def test_required_fields_cannot_be_omitted_on_create(self):
        for field in ("title", "author", "inventory", "daily_fee"):
            with self.subTest(field=field):
                payload = self.payload.copy()
                del payload[field]
                serializer = BookSerializer(data=payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn(field, serializer.errors)
        self.assertFalse(Book.objects.exists())

    def test_invalid_text_is_rejected(self):
        for field in ("title", "author"):
            for value in ("", " \t\n ", None, "A" * 256):
                with self.subTest(field=field, value=value):
                    payload = self.payload.copy()
                    payload[field] = value
                    serializer = BookSerializer(data=payload)
                    self.assertFalse(serializer.is_valid())
                    self.assertIn(field, serializer.errors)

    def test_inventory_requires_nonnegative_integer(self):
        for value in (-1, "1.5", "many", None):
            with self.subTest(value=value):
                self.payload["inventory"] = value
                serializer = BookSerializer(data=self.payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn("inventory", serializer.errors)

    def test_daily_fee_rejects_invalid_amounts(self):
        for value in ("-0.01", "1.001", "100000000.00", "free", "NaN", None):
            with self.subTest(value=value):
                self.payload["daily_fee"] = value
                serializer = BookSerializer(data=self.payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn("daily_fee", serializer.errors)

    def test_zero_inventory_and_fee_are_accepted(self):
        self.payload.update({"inventory": 0, "daily_fee": "0.00"})
        serializer = BookSerializer(data=self.payload)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        book = serializer.save()
        self.assertEqual(book.inventory, 0)
        self.assertEqual(book.daily_fee, Decimal("0.00"))

    def test_cover_accepts_only_supported_choices(self):
        for cover in ("HARD", "SOFT"):
            with self.subTest(cover=cover):
                self.payload["cover"] = cover
                serializer = BookSerializer(data=self.payload)
                self.assertTrue(serializer.is_valid(), serializer.errors)

        for cover in ("hard", "NONE", "", None):
            with self.subTest(cover=cover):
                self.payload["cover"] = cover
                serializer = BookSerializer(data=self.payload)
                self.assertFalse(serializer.is_valid())
                self.assertIn("cover", serializer.errors)

    def test_id_is_read_only(self):
        self.payload["id"] = 999999
        serializer = BookSerializer(data=self.payload)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("id", serializer.validated_data)
        book = serializer.save()
        self.assertNotEqual(book.pk, 999999)
        self.assertEqual(serializer.data["id"], book.pk)

    def test_partial_update_preserves_omitted_fields_and_id(self):
        book = Book.objects.create(**self.payload)
        original_id = book.pk
        serializer = BookSerializer(
            book,
            data={
                "id": 999999,
                "daily_fee": "2.75",
                "is_active": False,
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        book.refresh_from_db()
        self.assertEqual(book.pk, original_id)
        self.assertEqual(book.daily_fee, Decimal("2.75"))
        self.assertFalse(book.is_active)
        self.assertEqual(book.title, self.payload["title"])
        self.assertEqual(book.author, self.payload["author"])
        self.assertEqual(book.inventory, 3)
        self.assertEqual(Book.objects.count(), 1)

    def test_full_update_requires_required_fields(self):
        book = Book.objects.create(**self.payload)
        serializer = BookSerializer(book, data={"title": "Updated"})

        self.assertFalse(serializer.is_valid())
        for field in ("author", "inventory", "daily_fee"):
            self.assertIn(field, serializer.errors)
        book.refresh_from_db()
        self.assertEqual(book.title, "The Hobbit")

    def test_representation_includes_catalog_fields_and_decimal_string(self):
        book = Book.objects.create(**self.payload)
        serializer = BookSerializer(book)

        self.assertEqual(
            serializer.data,
            {
                "id": book.pk,
                "title": "The Hobbit",
                "author": "J. R. R. Tolkien",
                "cover": "HARD",
                "inventory": 3,
                "daily_fee": "1.50",
                "is_active": True,
            },
        )


class BookFilterSerializerTests(SimpleTestCase):
    def test_empty_query_does_not_add_boolean_filters(self):
        serializer = BookFilterSerializer(data=QueryDict("").dict())

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {})

    def test_text_filter_does_not_add_missing_boolean_filters(self):
        serializer = BookFilterSerializer(data=QueryDict("title=Hobbit").dict())

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {"title": "Hobbit"})

    def test_boolean_query_values_are_converted(self):
        cases: tuple[tuple[str, bool], ...] = (
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
        )
        for field in ("is_active", "available"):
            for raw_value, expected in cases:
                with self.subTest(field=field, value=raw_value):
                    query = QueryDict(f"{field}={raw_value}")
                    serializer = BookFilterSerializer(data=query.dict())
                    self.assertTrue(serializer.is_valid(), serializer.errors)
                    self.assertIs(serializer.validated_data[field], expected)
                    self.assertEqual(set(serializer.validated_data), {field})

    def test_invalid_boolean_filters_are_rejected(self):
        for field in ("is_active", "available"):
            for value in ("maybe", "2", "", None):
                with self.subTest(field=field, value=value):
                    serializer = BookFilterSerializer(data={field: value})
                    self.assertFalse(serializer.is_valid())
                    self.assertIn(field, serializer.errors)

    def test_combined_filters_are_preserved_and_text_is_trimmed(self):
        serializer = BookFilterSerializer(
            data={
                "title": " Hobbit ",
                "author": " Tolkien ",
                "cover": "SOFT",
                "is_active": "true",
                "available": "false",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data,
            {
                "title": "Hobbit",
                "author": "Tolkien",
                "cover": "SOFT",
                "is_active": True,
                "available": False,
            },
        )

    def test_blank_text_filters_are_rejected(self):
        for field in ("title", "author"):
            with self.subTest(field=field):
                serializer = BookFilterSerializer(data={field: " \t "})
                self.assertFalse(serializer.is_valid())
                self.assertIn(field, serializer.errors)

    def test_cover_filter_validates_choices(self):
        for cover in ("HARD", "SOFT"):
            with self.subTest(cover=cover):
                serializer = BookFilterSerializer(data={"cover": cover})
                self.assertTrue(serializer.is_valid(), serializer.errors)

        for cover in ("hard", "NONE", ""):
            with self.subTest(cover=cover):
                serializer = BookFilterSerializer(data={"cover": cover})
                self.assertFalse(serializer.is_valid())
                self.assertIn("cover", serializer.errors)
