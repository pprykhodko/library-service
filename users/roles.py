from django.contrib.auth.models import Group
from django.db import transaction

CUSTOMER = "customer"
EMPLOYEE = "employee"
ADMIN = "admin"
ROLE_CHOICES = (
    (CUSTOMER, "Customer"),
    (EMPLOYEE, "Employee"),
    (ADMIN, "Administrator")
)
ROLE_GROUPS = {
    CUSTOMER: "Customers",
    EMPLOYEE: "Employees"
}


def get_role(user):
    if user.is_superuser:
        return ADMIN

    names = set(user.groups.values_list("name", flat=True))

    if "Employees" in names:
        return EMPLOYEE

    if "Customers" in names:
        return CUSTOMER

    return None


def assign_role(user, role):
    """Internal operation; callers must authorize role changes first."""
    if role not in dict(ROLE_CHOICES):
        raise ValueError("Unknown role")

    database = user._state.db or "default"

    with transaction.atomic(using=database):
        user.is_superuser = role == ADMIN
        user.is_staff = role == ADMIN
        user.save(using=database, update_fields=["is_superuser", "is_staff"])
        user.groups.remove(
            *Group.objects.using(database).filter(name__in=ROLE_GROUPS.values())
        )

        if role in ROLE_GROUPS:
            group, _ = Group.objects.using(database).get_or_create(
                name=ROLE_GROUPS[role]
            )
            user.groups.add(group)
