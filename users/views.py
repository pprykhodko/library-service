from django.contrib.auth import get_user_model, update_session_auth_hash
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from users.permissions import CanManageOwnProfile, CanReadCustomers
from users.serializers import (
    ChangePasswordSerializer,
    CustomerSerializer,
    UserSerializer
)


class CreateUserView(generics.CreateAPIView):
    serializer_class = UserSerializer
    permission_classes = (AllowAny,)


class ManageUserView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = (IsAuthenticated, CanManageOwnProfile)

    def get_object(self):
        return self.request.user


class ChangePasswordView(generics.GenericAPIView):
    serializer_class = ChangePasswordSerializer
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        update_session_auth_hash(request, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerDetailView(generics.RetrieveAPIView):
    serializer_class = CustomerSerializer
    permission_classes = (IsAuthenticated, CanReadCustomers)

    def get_queryset(self):
        users = get_user_model().objects.all()

        if self.request.user.is_superuser:
            return users

        return (
            users.filter(groups__name="Customers", is_superuser=False)
            .exclude(groups__name="Employees")
        )
