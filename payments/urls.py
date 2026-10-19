from django.urls import include, path
from rest_framework.routers import SimpleRouter

from payments.views import (
    CancelPaymentView, PaymentViewSet, StripeWebhookView, SuccessPaymentView,
)

router = SimpleRouter()
router.register("", PaymentViewSet, basename="payments")

urlpatterns = [
    path("success/", SuccessPaymentView.as_view(), name="success"),
    path("cancel/", CancelPaymentView.as_view(), name="cancel"),
    path("webhook/", StripeWebhookView.as_view(), name="webhook"),
    path("", include(router.urls)),
]
app_name = "payments"
