from django.db import migrations


def create_roles(apps, schema_editor):
    database = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("users", "User")
    customers, _ = Group.objects.using(database).get_or_create(name="Customers")
    Group.objects.using(database).get_or_create(name="Employees")
    for user in User.objects.using(database).filter(is_superuser=False).exclude(groups__name="Employees"):
        user.groups.add(customers)


class Migration(migrations.Migration):
    dependencies = [("users", "0001_initial")]
    operations = [migrations.RunPython(create_roles, migrations.RunPython.noop)]
