from rest_framework import serializers

from payments.models import Payment


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = (
            "id",
            "borrowing",
            "status",
            "type",
            "session_url",
            "session_id",
            "money_to_pay",
        )
        read_only_fields = fields


class SessionQuerySerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=255)


class PaymentResultSerializer(serializers.Serializer):
    status = serializers.CharField()
    detail = serializers.CharField()
