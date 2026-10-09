from django import forms
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from users.models import User
from users.roles import CUSTOMER, ROLE_CHOICES, get_role


class RoleFormMixin:
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
