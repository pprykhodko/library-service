from rest_framework.permissions import BasePermission

from users.roles import ADMIN, CUSTOMER, EMPLOYEE, get_role


class CanAccessBorrowings(BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False

        role = get_role(request.user)

        if view.action == "create":
            return role in (CUSTOMER, ADMIN)

        if view.action == "return_book":
            return role in (EMPLOYEE, ADMIN)

        return role in (CUSTOMER, EMPLOYEE, ADMIN)
