from django.urls import path, include
from rest_framework import routers

from books.views import BookViewSet

router = routers.SimpleRouter()

router.register("", BookViewSet, basename="books")

urlpatterns = [path("", include(router.urls))]

app_name = "books"
