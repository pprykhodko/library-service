import stripe
from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from borrowings.pagination import BorrowingPagination
from payments.models import Payment
from payments.serializers import (
    PaymentResultSerializer,
    PaymentSerializer,
    SessionQuerySerializer
)
from payments.services import (
    PaymentProviderUnavailable,
    checkout,
    confirm_session,
    renew_checkout,
    retrieve_session
)
from users.roles import ADMIN, CUSTOMER, EMPLOYEE, get_role


@extend_schema(tags=["payments"])
class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Payment.objects.select_related("borrowing")
    serializer_class = PaymentSerializer
    permission_classes = (permissions.IsAuthenticated,)
    pagination_class = BorrowingPagination

    def get_queryset(self):
        queryset = super().get_queryset()

        if getattr(self, "swagger_fake_view", False):
            return queryset.none()

        user = self.request.user

        if not user.is_authenticated:
            return queryset.none()

        role = get_role(user)

        if role in (EMPLOYEE, ADMIN):
            return queryset

        if role == CUSTOMER:
            return queryset.filter(borrowing__user=user)

        return queryset.none()

    @extend_schema(
        request=None,
        responses=PaymentSerializer,
        description="Owner or administrator: create or reuse a Stripe checkout link."
    )
    @action(detail=True, methods=["post"])
    def checkout(self, request, pk=None):
        payment = self.get_object()

        if not request.user.is_superuser and (
            get_role(request.user) != CUSTOMER
            or payment.borrowing.user_id != request.user.pk
        ):
            self.permission_denied(request)

        return Response(self.get_serializer(checkout(payment)).data)

    @extend_schema(
        request=None,
        responses=PaymentSerializer,
        description="Owner or administrator: replace an expired Stripe session. "
                    "The payment amount and book inventory remain unchanged."
    )
    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        payment = self.get_object()

        if not request.user.is_superuser and (
            get_role(request.user) != CUSTOMER
            or payment.borrowing.user_id != request.user.pk
        ):
            self.permission_denied(request)

        return Response(self.get_serializer(renew_checkout(payment)).data)


@extend_schema(tags=["payments"])
class SuccessPaymentView(APIView):
    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        parameters=[SessionQuerySerializer],
        responses=PaymentResultSerializer
    )
    def get(self, request):
        query = SessionQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        payment = confirm_session(retrieve_session(query.validated_data["session_id"]))

        return Response({
            "status": payment.status,
            "detail": (
                "Payment confirmed." if payment.status == Payment.Status.PAID
                else "Payment has not been confirmed by Stripe yet"
            )
        })


@extend_schema(tags=["payments"])
class CancelPaymentView(APIView):
    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)

    @extend_schema(responses=PaymentResultSerializer)
    def get(self, request):
        return Response({
            "status": "cancelled",
            "detail": (
                "Checkout paused. Payment status is unchanged; "
                "use the same link to resume."
            )
        })


@extend_schema(tags=["payments"])
class StripeWebhookView(APIView):
    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)

    @extend_schema(request=None, responses={200: None})
    def post(self, request):
        if not settings.STRIPE_WEBHOOK_SECRET:
            raise PaymentProviderUnavailable("Stripe webhook is not configured")

        try:
            event = stripe.Webhook.construct_event(
                request.body, request.headers.get("Stripe-Signature", ""),
                settings.STRIPE_WEBHOOK_SECRET,
            ).to_dict()

        except (ValueError, stripe.SignatureVerificationError):
            raise serializers.ValidationError(
                "Invalid webhook signature or payload"
            )

        if event["type"] in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.expired"
        ):
            session = event["data"]["object"]

            if session.get("metadata", {}).get("payment_id"):
                confirm_session(session)

        return Response(status=200)
