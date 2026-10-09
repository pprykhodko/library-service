from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from users.forms import AdminUserChangeForm, AdminUserCreationForm
from users.models import User
from users.roles import assign_role, get_role


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = AdminUserChangeForm
    add_form = AdminUserCreationForm
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("first_name", "last_name")}),
        ("Access", {"fields": ("is_active", "role")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": (
            "email", "first_name", "last_name", "password1", "password2", "role",
        )}),
    )
    readonly_fields = ("last_login", "date_joined")
    list_display = ("email", "first_name", "last_name", "display_role", "is_active")
    list_filter = ("is_active", "groups", "is_superuser")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)
    filter_horizontal = ()

    @admin.display(description="Role")
    def display_role(self, obj):
        return get_role(obj)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        assign_role(form.instance, form.cleaned_data["role"])

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)
