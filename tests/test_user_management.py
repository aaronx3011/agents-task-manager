"""Owner-managed users and password changes without email (#47)."""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import Client

from accounts.models import AccountEvent, Role, RoleChange
from accounts.services import role_of, set_role

User = get_user_model()
OLD = "an-Old-passphrase-123"
NEW = "a-Brand-new-passphrase-456"


@pytest.fixture
def owner(django_user_model):
    user = django_user_model.objects.create_user("boss", password=OLD, is_superuser=True)
    client = Client()
    client.force_login(user)
    return user, client


@pytest.fixture
def member(django_user_model):
    user = django_user_model.objects.create_user("worker", "w@example.test", OLD)
    set_role(user, Role.MEMBER, actor="test")
    return user


def patch(client, user, body):
    return client.patch(f"/api/v1/users/{user.pk}", body, content_type="application/json")


def events(kind):
    return AccountEvent.objects.filter(event_type=kind)


@pytest.mark.django_db
def test_owner_lists_users_with_roles(owner, member):
    _, client = owner
    response = client.get("/api/v1/users")
    assert response.status_code == 200
    rows = {row["username"]: row for row in response.json()}
    assert rows["boss"]["role"] == "owner" and rows["worker"]["role"] == "member"
    assert "password" not in response.content.decode()


@pytest.mark.django_db
def test_owner_changes_roles_with_audit(owner, member):
    _, client = owner
    response = patch(client, member, {"role": "viewer"})
    assert response.status_code == 200 and response.json()["role"] == "viewer"
    change = RoleChange.objects.filter(user=member).first()
    assert (change.old_role, change.new_role, change.actor) == ("member", "viewer", "boss")


@pytest.mark.django_db
def test_deactivation_ends_access_and_reactivation_restores_it(owner, member):
    _, client = owner
    session = Client()
    session.force_login(member)
    assert session.get("/api/v1/auth/me").status_code == 200

    assert patch(client, member, {"is_active": False}).json()["is_active"] is False
    assert session.get("/api/v1/auth/me").status_code in {401, 403}
    assert events("user.deactivated").get().subject_user == member

    assert patch(client, member, {"is_active": True}).json()["is_active"] is True
    assert events("user.reactivated").get().actor == "boss"
    assert role_of(User.objects.get(pk=member.pk)) == "member"


@pytest.mark.django_db
def test_last_active_owner_cannot_be_demoted_or_deactivated(owner, member):
    boss, client = owner
    assert patch(client, boss, {"role": "member"}).status_code == 409
    assert patch(client, boss, {"is_active": False}).status_code == 409
    assert patch(client, boss, {"role": "member", "is_active": False}).status_code == 409
    boss.refresh_from_db()
    assert boss.is_active and role_of(boss) == "owner"

    # An inactive second owner does not count; an active one does.
    patch(client, member, {"role": "owner"})
    patch(client, member, {"is_active": False})
    assert patch(client, boss, {"role": "member"}).status_code == 409
    patch(client, member, {"is_active": True})
    assert patch(client, boss, {"role": "member"}).status_code == 200


@pytest.mark.django_db
def test_failed_combined_update_changes_nothing(owner, member):
    boss, client = owner
    # Role change would succeed alone, but deactivating the last owner fails: roll back both.
    other = User.objects.create_user("other-owner", password=OLD)
    set_role(other, Role.OWNER, actor="test")
    other.is_active = False
    other.save()
    assert patch(client, boss, {"role": "owner", "is_active": False}).status_code == 409
    assert patch(client, member, {"role": "owner", "is_active": "maybe"}).status_code == 400
    assert role_of(member) == "member"


@pytest.mark.django_db
def test_owner_sets_a_password_and_existing_sessions_end(owner, member):
    _, client = owner
    session = Client()
    session.force_login(member)
    weak = client.post(
        f"/api/v1/users/{member.pk}/password", {"password": "123"}, content_type="application/json"
    )
    assert weak.status_code == 400
    response = client.post(
        f"/api/v1/users/{member.pk}/password", {"password": NEW}, content_type="application/json"
    )
    assert response.status_code == 204
    member.refresh_from_db()
    assert member.check_password(NEW)
    assert session.get("/api/v1/auth/me").status_code in {401, 403}
    event = events("password.set").get()
    assert event.actor == "boss" and NEW not in event.message


@pytest.mark.django_db
def test_users_change_their_own_password_with_the_current_one(member):
    client = Client()
    client.force_login(member)
    other_session = Client()
    other_session.force_login(member)

    def change(current, new):
        return client.post(
            "/api/v1/auth/password",
            {"current_password": current, "new_password": new},
            content_type="application/json",
        )

    assert change("wrong", NEW).status_code == 400
    assert change(OLD, OLD).status_code == 400
    assert change(OLD, NEW).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 200  # this session stays
    assert other_session.get("/api/v1/auth/me").status_code in {401, 403}
    assert events("password.changed").get().subject_user == member


@pytest.mark.django_db
def test_user_management_is_owner_only(member, api_client):
    client = Client()
    client.force_login(member)
    assert client.get("/api/v1/users").status_code == 403
    assert patch(client, member, {"role": "owner"}).status_code == 403
    assert api_client.get("/api/v1/users").status_code == 403
    assert Client().get("/api/v1/users").status_code in {401, 403}
    assert role_of(member) == "member"


@pytest.mark.django_db
def test_set_user_password_command_recovers_without_printing(member, monkeypatch, capsys):
    monkeypatch.setenv("USER_PASSWORD", NEW)
    call_command("set_user_password", "worker", "--no-input")
    member.refresh_from_db()
    assert member.check_password(NEW)
    assert events("password.set").get().actor == "set_user_password"
    out = capsys.readouterr()
    assert NEW not in out.out + out.err
    with pytest.raises(CommandError):
        call_command("set_user_password", "nobody", "--no-input")
    monkeypatch.setenv("USER_PASSWORD", "123")
    with pytest.raises(CommandError):
        call_command("set_user_password", "worker", "--no-input")
