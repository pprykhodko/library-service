from django.contrib.auth import get_user_model, password_validation
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field

from users.roles import ROLE_CHOICES

from users.roles import get_role


class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = (
            "id",
            "email",
            "password",
            "first_name",
            "last_name",
            "is_staff",
            "role"
        )
        read_only_fields = ("id", "is_staff", "role")
        extra_kwargs = {
            "password": {
                "write_only": True,
                "min_length": 8,
                "trim_whitespace": False
            }
        }

    @extend_schema_field(
        serializers.ChoiceField(
            choices=ROLE_CHOICES,
            allow_null=True)
    )
    def get_role(self, obj):
        return get_role(obj)

    def validate_email(self, value):
        value = get_user_model().objects.normalize_email(value)
        users = get_user_model().objects.filter(email__iexact=value)

        if self.instance:
            users = users.exclude(pk=self.instance.pk)

        if users.exists():
            raise serializers.ValidationError("A user with this email already exists")

        return value

    def validate(self, attrs):
        if "password" in attrs:
            candidate = get_user_model()(**{
                name: attrs.get(name, getattr(self.instance, name, ""))
                for name in ("email", "first_name", "last_name")
            })
            password_validation.validate_password(attrs["password"], candidate)

        return attrs

    def create(self, validated_data):
        return get_user_model().objects.create_user(**validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)

        for name, value in validated_data.items():
            setattr(instance, name, value)

        if password:
            instance.set_password(password)

        instance.save()

        return instance


class ProfileSerializer(UserSerializer):
    class Meta(UserSerializer.Meta):
        fields = tuple(
            field for field in UserSerializer.Meta.fields if field != "password"
        )

    def validate(self, attrs):
        if "password" in self.initial_data:
            raise serializers.ValidationError({
                "password": "Use /users/me/password/ with your current password."
            })

        return super().validate(attrs)


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = ("id", "email", "first_name", "last_name")
        read_only_fields = fields


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_old_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Incorrect current password")

        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)

        return value
