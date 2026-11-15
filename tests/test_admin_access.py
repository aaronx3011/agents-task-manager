"""Django admin is superuser-only operations (#53)."""

import importlib

import pytest
from django.test import Client
from django.urls import clear_url_caches

from accounts.models import Role
from accounts.services import set_role


def signed_in(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("flags", "role"),
    [
        ({"is_staff": True}, Role.MEMBER),  # legacy staff account
        ({"is_staff": True}, Role.OWNER),  # role owners are not admins by default
        ({}, Role.OWNER),
        ({}, Role.VIEWER),
    ],
)
def test_non_superusers_cannot_use_admin(django_user_model, flags, role):
    user = django_user_model.objects.create_user("not-root", password="pw", **flags)
    set_role(user, role, actor="test")
    client = signed_in(user)
    index = client.get("/admin/")
    assert index.status_code == 302 and "/admin/login/" in index["Location"]
    assert client.get("/admin/accounts/userprofile/").status_code == 302
    login = Client().post("/admin/login/", {"username": "not-root", "password": "pw"})
    assert login.status_code == 200  # the form re-renders with an error
    assert "_auth_user_id" not in login.wsgi_request.session


@pytest.mark.django_db
def test_superusers_keep_admin(django_user_model):
    root = django_user_model.objects.create_superuser("root", password="pw")
    client = signed_in(root)
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/accounts/userprofile/").status_code == 200


@pytest.fixture
def reload_urls(settings):
    def apply(**overrides):
        for name, value in overrides.items():
            setattr(settings, name, value)
        import config.urls

        importlib.reload(config.urls)
        clear_url_caches()

    yield apply
    settings.ADMIN_ENABLED, settings.ADMIN_PATH, settings.DEBUG = True, "admin/", False
    import config.urls

    importlib.reload(config.urls)
    clear_url_caches()


@pytest.mark.django_db
def test_admin_can_move_or_be_disabled(django_user_model, reload_urls):
    root = signed_in(django_user_model.objects.create_superuser("root", password="pw"))
    reload_urls(ADMIN_PATH="ops-hidden/")
    assert root.get("/admin/").status_code == 404
    assert root.get("/ops-hidden/").status_code == 200
    assert Client().get("/api/v1/meta").json()["admin_url"] == "/ops-hidden/"
    reload_urls(ADMIN_ENABLED=False)
    assert root.get("/ops-hidden/").status_code == 404
    assert Client().get("/api/v1/meta").json()["admin_url"] is None


@pytest.mark.django_db
def test_browsable_api_login_only_in_debug(reload_urls):
    reload_urls(DEBUG=False)
    assert Client().get("/api-auth/login/").status_code == 404
    reload_urls(DEBUG=True)
    assert Client().get("/api-auth/login/").status_code == 200
