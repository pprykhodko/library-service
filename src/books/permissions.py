from rest_framework.permissions import BasePermission, SAFE_METHODS

from users.roles import ADMIN, EMPLOYEE, get_role


class CanManageBooks(BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False

        return request.method in SAFE_METHODS or get_role(request.user) in (
            EMPLOYEE,
            ADMIN,
        )
