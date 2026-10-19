from django.contrib import admin

from payments.models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "borrowing", "status", "type", "money_to_pay")
    list_filter = ("status", "type")
    readonly_fields = (
        "borrowing",
        "status",
        "type",
        "money_to_pay",
        "session_id",
        "session_url",
        "checkout_key"
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
