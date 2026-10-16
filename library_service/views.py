from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView


@extend_schema(exclude=True)
class APIRootView(APIView):
    permission_classes = (AllowAny,)

    def get(self, request):
        return Response({
            "users": reverse("users:register", request=request),
            "books": reverse("books:books-list", request=request),
            "borrowings": reverse("borrowings:borrowings-list", request=request),
            "payments": reverse("payments:payments-list", request=request),
        })
