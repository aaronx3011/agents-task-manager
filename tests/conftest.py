import pytest
from django.test import Client

from coordinator.models import Agent, Project


@pytest.fixture(autouse=True)
def fast_test_passwords(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def project(db):
    return Project.objects.create(slug="coordinator", name="Coordinator")


@pytest.fixture
def agent_clients(db):
    clients = []
    for name in ["alpha", "beta"]:
        agent = Agent(name=name)
        key = agent.set_new_api_key()
        agent.save()
        clients.append(Client(HTTP_AUTHORIZATION=f"Bearer {key}"))
    return clients


@pytest.fixture
def api_client(agent_clients):
    return agent_clients[0]
