from django.urls import path
from rest_framework_simplejwt.views import (
    TokenRefreshView,
    TokenVerifyView,
    TokenObtainPairView
)

from users.views import (
    CreateUserView,
    ManageUserView,
    ChangePasswordView,
    CustomerDetailView,
)


urlpatterns = [
    path("", CreateUserView.as_view(), name="register"),
    path("token/", TokenObtainPairView.as_view(), name="token-obtain"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("token/verify/", TokenVerifyView.as_view(), name="token-verify"),
    path("me/password/", ChangePasswordView.as_view(), name="change-password"),
    path("<int:pk>/", CustomerDetailView.as_view(), name="customer-detail"),
    path("me/", ManageUserView.as_view(), name="manage"),
]

app_name = "users"
