from django.conf import settings
from django.db import migrations


def initial_role(user):
    # Mirrors accounts.models.role_for_flags; historical models cannot import it safely.
    if user.is_superuser:
        return "owner"
    if user.is_staff:
        return "member"
    return "viewer"


def backfill_roles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    UserProfile = apps.get_model("accounts", "UserProfile")
    RoleChange = apps.get_model("accounts", "RoleChange")
    existing = set(UserProfile.objects.values_list("user_id", flat=True))
    for user in User.objects.exclude(pk__in=existing).order_by("pk").iterator():
        role = initial_role(user)
        UserProfile.objects.create(user=user, role=role)
        RoleChange.objects.create(
            user=user, old_role="", new_role=role, actor="migration", reason="backfill from staff"
        )


def remove_backfill(apps, schema_editor):
    apps.get_model("accounts", "RoleChange").objects.filter(actor="migration").delete()
    apps.get_model("accounts", "UserProfile").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_user_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(backfill_roles, remove_backfill)]
