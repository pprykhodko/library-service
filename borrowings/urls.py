from django.urls import path, include
from rest_framework import routers

from borrowings.views import BorrowingViewSet

router = routers.SimpleRouter()

router.register("", BorrowingViewSet, basename="borrowings")

urlpatterns = [path("", include(router.urls))]

app_name = "borrowings"
