from django import forms
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from users.models import User
from users.roles import CUSTOMER, ROLE_CHOICES, get_role


class RoleFormMixin:
    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"])
        users = User.objects.filter(email__iexact=email)

        if self.instance.pk:
            users = users.exclude(pk=self.instance.pk)

        if users.exists():
            raise forms.ValidationError("A user with this email already exists")

        return email

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].initial = get_role(self.instance) if (
            self.instance.pk
        ) else CUSTOMER


class AdminUserCreationForm(RoleFormMixin, UserCreationForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, initial=CUSTOMER)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "first_name", "last_name")


class AdminUserChangeForm(RoleFormMixin, UserChangeForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES)

    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"
