from django.contrib.auth import get_user_model, update_session_auth_hash
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, extend_schema_view

from users.permissions import CanManageOwnProfile, CanReadCustomers
from users.serializers import (
    ChangePasswordSerializer,
    CustomerSerializer,
    UserSerializer,
    ProfileSerializer
)


@extend_schema(
    tags=["users"],
    summary="Register a customer"
)
class CreateUserView(generics.CreateAPIView):
    serializer_class = UserSerializer
    permission_classes = (AllowAny,)


@extend_schema_view(
    get=extend_schema(
        tags=["users"],
        summary="Read your profile",
        responses=ProfileSerializer
    ),
    put=extend_schema(
        tags=["users"],
        summary="Update your profile",
        description=(
            "Customers may edit their profile. Employees have read-only access. "
            "All roles must change passwords through /users/me/password/."
        ),
        request=ProfileSerializer,
        responses=ProfileSerializer
    ),
    patch=extend_schema(
        tags=["users"],
        summary="Partially update your profile",
        description=(
            "Customers may edit their profile. Employees have read-only access. "
            "All roles must change passwords through /users/me/password/."
        ),
        request=ProfileSerializer,
        responses=ProfileSerializer
    )
)
class ManageUserView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileSerializer
    permission_classes = (IsAuthenticated, CanManageOwnProfile)

    def get_object(self):
        return self.request.user


class ChangePasswordView(generics.GenericAPIView):
    serializer_class = ChangePasswordSerializer
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        tags=["users"],
        summary="Change your password",
        description=(
            "Requires the current password. Available to all authenticated roles."
        ),
        responses={204: None}
    )
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        update_session_auth_hash(request, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=["users"],
    summary="Read a customer profile",
    description=(
        "Employees may read customers only; administrators may read any user. "
        "Read-only endpoint."
    )
)
class CustomerDetailView(generics.RetrieveAPIView):
    serializer_class = CustomerSerializer
    permission_classes = (IsAuthenticated, CanReadCustomers)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return get_user_model().objects.none()

        users = get_user_model().objects.all()

        if self.request.user.is_superuser:
            return users

        return (
            users.filter(groups__name="Customers", is_superuser=False)
            .exclude(groups__name="Employees")
        )
