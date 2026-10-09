from rest_framework.permissions import BasePermission, SAFE_METHODS

from users.roles import ADMIN, CUSTOMER, EMPLOYEE, get_role


class CanManageOwnProfile(BasePermission):
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False

        return (
                request.method in SAFE_METHODS
                or get_role(request.user) in (CUSTOMER, ADMIN)
        )


class CanReadCustomers(BasePermission):
    def has_permission(self, request, view):
        return (
                request.user.is_authenticated and get_role(request.user)
                in (EMPLOYEE, ADMIN)
        )
