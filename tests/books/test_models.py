from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from books.models import Book


class BookModelTests(TestCase):
    def setUp(self) -> None:
        self.book = Book(
            title="The Hobbit",
            author="J. R. R. Tolkien",
            inventory=3,
            daily_fee=Decimal("1.50"),
        )

    def test_valid_book_can_be_saved_with_defaults(self):
        self.book.full_clean()
        self.book.save()
        self.book.refresh_from_db()

        self.assertEqual(self.book.title, "The Hobbit")
        self.assertEqual(self.book.author, "J. R. R. Tolkien")
        self.assertEqual(self.book.inventory, 3)
        self.assertEqual(self.book.daily_fee, Decimal("1.50"))
        self.assertEqual(self.book.cover, Book.Cover.HARD)
        self.assertTrue(self.book.is_active)

    def test_clean_trims_title_and_author_but_preserves_internal_spaces(self):
        self.book.title = "  The Hobbit \t"
        self.book.author = "\n J. R. R. Tolkien  "
        self.book.full_clean()
        self.book.save()
        self.book.refresh_from_db()

        self.assertEqual(self.book.title, "The Hobbit")
        self.assertEqual(self.book.author, "J. R. R. Tolkien")

    def test_blank_or_whitespace_title_and_author_are_invalid(self):
        for field in ("title", "author"):
            original = getattr(self.book, field)
            for value in ("", "   ", "\t\n"):
                with self.subTest(field=field, value=value):
                    setattr(self.book, field, value)
                    with self.assertRaises(ValidationError) as error:
                        self.book.full_clean()
                    self.assertIn(field, error.exception.message_dict)
            setattr(self.book, field, original)

    def test_title_and_author_length_limits(self):
        for field in ("title", "author"):
            original = getattr(self.book, field)
            with self.subTest(field=field):
                setattr(self.book, field, "A" * 255)
                self.book.full_clean()
                setattr(self.book, field, "A" * 256)
                with self.assertRaises(ValidationError) as error:
                    self.book.full_clean()
                self.assertIn(field, error.exception.message_dict)
            setattr(self.book, field, original)

    def test_zero_inventory_and_free_book_are_allowed(self):
        self.book.inventory = 0
        self.book.daily_fee = Decimal("0.00")
        self.book.full_clean()
        self.book.save()
        self.book.refresh_from_db()

        self.assertEqual(self.book.inventory, 0)
        self.assertEqual(self.book.daily_fee, Decimal("0.00"))

    def test_negative_inventory_is_invalid(self):
        self.book.inventory = -1
        with self.assertRaises(ValidationError) as error:
            self.book.full_clean()
        self.assertIn("inventory", error.exception.message_dict)

    def test_negative_daily_fee_is_invalid(self):
        self.book.daily_fee = Decimal("-0.01")
        with self.assertRaises(ValidationError) as error:
            self.book.full_clean()
        self.assertIn("daily_fee", error.exception.message_dict)

    def test_daily_fee_precision_and_size_limits(self):
        self.book.daily_fee = Decimal("99999999.99")
        self.book.full_clean()

        for amount in (Decimal("1.001"), Decimal("100000000.00")):
            with self.subTest(amount=amount):
                self.book.daily_fee = amount
                with self.assertRaises(ValidationError) as error:
                    self.book.full_clean()
                self.assertIn("daily_fee", error.exception.message_dict)

    def test_both_cover_choices_are_valid(self):
        for cover in (Book.Cover.HARD, Book.Cover.SOFT):
            with self.subTest(cover=cover):
                self.book.cover = cover
                self.book.full_clean()
                self.book.save()
                self.book.refresh_from_db()
                self.assertEqual(self.book.cover, cover)

    def test_invalid_cover_is_rejected_by_validation(self):
        self.book.cover = "NONE"
        with self.assertRaises(ValidationError) as error:
            self.book.full_clean()
        self.assertIn("cover", error.exception.message_dict)

    def test_database_rejects_invalid_updates_without_model_validation(self):
        self.book.save()
        for changes in (
            {"inventory": -1},
            {"daily_fee": Decimal("-0.01")},
            {"cover": "NONE"},
        ):
            with self.subTest(changes=changes):
                # update() bypasses full_clean(); database constraints must still hold.
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Book.objects.filter(pk=self.book.pk).update(**changes)
                self.book.refresh_from_db()
                self.assertEqual(self.book.inventory, 3)
                self.assertEqual(self.book.daily_fee, Decimal("1.50"))
                self.assertEqual(self.book.cover, Book.Cover.HARD)

    def test_string_representation_contains_title_and_author(self):
        self.assertEqual(str(self.book), "The Hobbit - J. R. R. Tolkien")
