"""User roles: owner/member/viewer with audited assignment (#42)."""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from accounts.models import Role, RoleChange, UserProfile
from accounts.services import AccountError, has_role, role_of, set_role

User = get_user_model()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("flags", "role"),
    [
        ({"is_superuser": True, "is_staff": True}, Role.OWNER),
        ({"is_staff": True}, Role.MEMBER),
        ({}, Role.VIEWER),
    ],
)
def test_new_accounts_get_an_audited_initial_role(flags, role):
    user = User.objects.create_user("someone", **flags)
    assert user.profile.role == role
    change = RoleChange.objects.get(user=user)
    assert (change.old_role, change.new_role, change.actor) == ("", role, "system")


@pytest.mark.django_db
def test_set_role_audits_changes_and_ignores_noops():
    User.objects.create_superuser("root", password="pw")
    user = User.objects.create_user("dev", is_staff=True)
    set_role(user, Role.VIEWER, actor="root", reason="read only for now")
    set_role(user, Role.VIEWER, actor="root")
    changes = list(RoleChange.objects.filter(user=user).order_by("pk"))
    assert [(c.old_role, c.new_role, c.actor) for c in changes] == [
        ("", Role.MEMBER, "system"),
        (Role.MEMBER, Role.VIEWER, "root"),
    ]
    with pytest.raises(AccountError):
        set_role(user, "admin", actor="root")
    with pytest.raises(AccountError):
        set_role(user, Role.MEMBER, actor=" ")


@pytest.mark.django_db
def test_last_owner_cannot_be_demoted():
    first = User.objects.create_superuser("first", password="pw")
    with pytest.raises(AccountError) as exc:
        set_role(first, Role.MEMBER, actor="first")
    assert exc.value.status == 409
    second = User.objects.create_user("second")
    set_role(second, Role.OWNER, actor="first")
    set_role(first, Role.MEMBER, actor="second")
    assert UserProfile.objects.get(user=first).role == Role.MEMBER


@pytest.mark.django_db
def test_role_helpers_rank_roles_and_ignore_inactive_users():
    owner = User.objects.create_superuser("own", password="pw")
    viewer = User.objects.create_user("view")
    assert has_role(owner, Role.MEMBER) and has_role(owner, Role.OWNER)
    assert has_role(viewer, Role.VIEWER) and not has_role(viewer, Role.MEMBER)
    viewer.is_active = False
    assert role_of(viewer) is None and not has_role(viewer, Role.VIEWER)


@pytest.mark.django_db
def test_create_owner_creates_and_promotes_without_printing_the_password(monkeypatch, capsys):
    secret = "a-Long-unique-passphrase-91"
    monkeypatch.setenv("OWNER_PASSWORD", secret)
    call_command("create_owner", "alice", "--email", "alice@example.test", "--no-input")
    alice = User.objects.get(username="alice")
    assert alice.check_password(secret) and alice.is_staff and not alice.is_superuser
    assert role_of(alice) == Role.OWNER
    assert RoleChange.objects.filter(user=alice, actor="create_owner").exists()

    bob = User.objects.create_user("bob", password="old")
    call_command("create_owner", "bob", "--no-input")
    bob.refresh_from_db()
    assert role_of(bob) == Role.OWNER and bob.is_staff and bob.check_password("old")

    out = capsys.readouterr()
    assert secret not in out.out + out.err


@pytest.mark.django_db
def test_create_owner_rejects_missing_or_weak_passwords(monkeypatch):
    monkeypatch.delenv("OWNER_PASSWORD", raising=False)
    with pytest.raises(CommandError):
        call_command("create_owner", "carol", "--no-input")
    monkeypatch.setenv("OWNER_PASSWORD", "123")
    with pytest.raises(CommandError):
        call_command("create_owner", "carol", "--no-input")
    assert not User.objects.filter(username="carol").exists()


@pytest.mark.django_db
def test_admin_role_edits_are_audited_and_keep_an_owner():
    root = User.objects.create_superuser("root", password="pw")
    member = User.objects.create_user("member", is_staff=True)
    client = Client()
    client.force_login(root)
    url = f"/admin/accounts/userprofile/{member.profile.pk}/change/"
    assert client.post(url, {"role": "owner"}).status_code == 302
    change = RoleChange.objects.filter(user=member).first()
    assert (change.old_role, change.new_role, change.actor) == ("member", "owner", "root")

    set_role(member, Role.MEMBER, actor="root")
    response = client.post(
        f"/admin/accounts/userprofile/{root.profile.pk}/change/", {"role": "viewer"}, follow=True
    )
    assert "Cannot remove the last owner" in response.content.decode()
    assert UserProfile.objects.get(user=root).role == Role.OWNER
    assert client.get("/admin/accounts/rolechange/add/").status_code == 403


def migrate_to_latest():
    # Later transactional tests need every table, including newer migrations.
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_backfill_migration_maps_staff_flags_to_roles():
    executor = MigrationExecutor(connection)
    executor.migrate([("accounts", "0001_user_roles")])
    apps = executor.loader.project_state([("accounts", "0001_user_roles")]).apps
    HistoricalUser = apps.get_model("auth", "User")
    for name, flags in [
        ("root", {"is_superuser": True, "is_staff": True}),
        ("staff", {"is_staff": True}),
        ("plain", {}),
    ]:
        HistoricalUser.objects.create(username=name, password="!", **flags)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([("accounts", "0002_backfill_roles")])

    roles = dict(UserProfile.objects.values_list("user__username", "role"))
    assert roles == {"root": "owner", "staff": "member", "plain": "viewer"}
    assert set(RoleChange.objects.values_list("actor", flat=True)) == {"migration"}
    migrate_to_latest()
