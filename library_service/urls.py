from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from library_service.views import APIRootView

urlpatterns = [
    path("", APIRootView.as_view(), name="api-root"),
    path('admin/', admin.site.urls),
    path("users/", include("users.urls")),
    path("books/", include("books.urls")),
    path("borrowings/", include("borrowings.urls")),
    path("payments/", include("payments.urls")),
    path(
        "api/schema/",
        SpectacularAPIView.as_view(),
        name="schema"
    ),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui"
    )
]
