import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from coordinator import services
from coordinator.models import Agent, Task, TaskEvent, Worker

pytestmark = pytest.mark.django_db


@pytest.fixture
def bound_worker(agent_clients):
    alpha = Agent.objects.get(name="alpha")
    worker = services.provision_worker("worker-a", alpha)
    worker.opencode_url = "http://127.0.0.1:4096"
    worker.save()
    return worker


@pytest.fixture
def staff_client(client):
    user = User.objects.create_user("root", password="pw", is_staff=True)
    client.force_login(user)
    return client


def test_provision_worker_creates_explicit_binding(bound_worker):
    assert bound_worker.is_enabled
    assert bound_worker.agent.name == "alpha"
    # The worker itself holds no credential: binding reuses the agent's key.
    assert not [
        field.name
        for field in Worker._meta.concrete_fields
        if "key" in field.name or "password" in field.name
    ]


def test_provision_worker_rejects_duplicates_and_inactive_agents(agent_clients, project):
    alpha = Agent.objects.get(name="alpha")
    services.provision_worker("worker-a", alpha)
    with pytest.raises(services.TaskError) as conflict:
        services.provision_worker("worker-a", Agent.objects.get(name="beta"))
    assert conflict.value.status == 409
    beta = Agent.objects.get(name="beta")
    services.provision_worker("worker-b", beta)
    with pytest.raises(services.TaskError):
        services.provision_worker("worker-b", Agent.objects.get(name="alpha"))
    gamma = Agent(name="gamma")
    gamma.save()
    gamma.is_active = False
    gamma.save(update_fields=["is_active"])
    with pytest.raises(services.TaskError) as inactive_error:
        services.provision_worker("worker-c", gamma)
    assert inactive_error.value.status == 400
    with pytest.raises(services.TaskError) as unsaved:
        services.provision_worker("worker-d", Agent(name="ghost"))
    assert unsaved.value.status == 400


def test_provision_command_demotes_service_errors():
    with pytest.raises(CommandError):
        call_command("provision_worker", "worker-x", "--agent", "missing")


def test_worker_write_binding_rejects_spoofing(agent_clients, bound_worker):
    beta = Agent.objects.get(name="beta")
    with pytest.raises(services.TaskError) as spoof:
        services.verify_worker_write("worker-a", beta)
    assert spoof.value.status == 403
    with pytest.raises(services.TaskError) as missing:
        services.verify_worker_write("worker-other", beta)
    assert missing.value.status == 404
    assert services.verify_worker_write("worker-a", Agent.objects.get(name="alpha"))


def test_disabled_worker_blocks_writes(agent_clients, bound_worker):
    call_command("deactivate_worker", "worker-a", "paused for maintenance")
    bound_worker.refresh_from_db()
    assert not bound_worker.is_enabled
    with pytest.raises(services.TaskError) as denied:
        services.verify_worker_write("worker-a", Agent.objects.get(name="alpha"))
    assert denied.value.status == 403


def test_online_derives_from_configured_freshness(bound_worker, settings):
    settings.WORKER_OFFLINE_SECONDS = 10
    now = timezone.now()
    bound_worker.last_sync_at = now
    assert bound_worker.is_online(now)
    bound_worker.last_sync_at = now - timezone.timedelta(seconds=10)
    assert bound_worker.is_online(now)
    bound_worker.last_sync_at = now - timezone.timedelta(seconds=10, milliseconds=1)
    assert not bound_worker.is_online(now)
    bound_worker.last_sync_at = None
    assert not bound_worker.is_online(now)
    settings.WORKER_OFFLINE_SECONDS = 60
    bound_worker.last_sync_at = now - timezone.timedelta(seconds=30)
    assert bound_worker.is_online(now)


def test_record_worker_meta_sanitizes_endpoint_and_updates_observables(bound_worker):
    services.record_worker_meta(
        bound_worker,
        meta={
            "hostname": "laptop",
            "opencode_url": "http://bridgeuser:secret@127.0.0.1:4096/",
            "protocol_version": 1,
            "opencode_version": "1.18.32",
            "directory": "/Users/me/project",
            "create_directory": "/Users/me/new",
            "connected": True,
            "stream_connected": True,
            "error": "provider DID fail",
            "models": [{"providerID": "openai", "modelID": "gpt", "name": "gpt"}],
            "default_model": {"providerID": "openai", "modelID": "gpt"},
            "session_limit": 50,
            "message_limit": 30,
            "dropped_events": 3,
        },
    )
    bound_worker.refresh_from_db()
    assert bound_worker.opencode_url == "http://127.0.0.1:4096"  # userinfo stripped
    assert bound_worker.protocol_version == 1
    assert bound_worker.connected and bound_worker.stream_connected
    assert "provider DID fail" in bound_worker.last_error
    assert bound_worker.last_error_at is not None
    assert bound_worker.dropped_events == 3
    assert bound_worker.last_sync_at is not None
    # Restart persistence: a fresh read (no shared state) sees a cleared error yet
    # still holds the other metadata, because unsent fields are never clobbered.
    fresh = Worker.objects.get(pk=bound_worker.pk)
    services.record_worker_meta(fresh, meta={"connected": False, "error": "", "dropped_events": 0})
    bound_worker.refresh_from_db()
    assert not bound_worker.connected
    assert bound_worker.last_error == "" and bound_worker.last_error_at is None
    assert bound_worker.hostname == "laptop"
    assert bound_worker.opencode_version == "1.18.32"
    assert bound_worker.model_catalog and bound_worker.default_model


def test_worker_heartbeat_never_renews_task_claims(agent_clients, project, bound_worker):
    response = agent_clients[0].post(
        "/api/tasks", {"title": "Follow-up", "project": "coordinator"}, format="json"
    )
    assert response.status_code == 201
    task_id = response.data["id"]
    assert (
        agent_clients[0].post(f"/api/tasks/{task_id}/claim", {}, format="json").status_code == 200
    )
    task = Task.objects.get(pk=task_id)
    before = task.heartbeat_at
    events_before = TaskEvent.objects.filter(task_id=task_id).count()
    services.record_worker_meta(bound_worker, meta={"connected": True})
    task.refresh_from_db()
    assert task.heartbeat_at == before  # worker sync never renews task ownership
    assert TaskEvent.objects.filter(task_id=task_id).count() == events_before


def test_worker_read_apis_are_authenticated(agent_clients, staff_client, bound_worker):
    from django.test import Client

    assert Client().get("/api/workers").status_code == 401  # anonymous client
    listed = agent_clients[0].get("/api/workers")
    assert listed.status_code == 200 and listed.data["count"] == 1
    item = listed.data["items"][0]
    assert item["name"] == "worker-a" and item["agent"] == "alpha"
    assert item["online"] is False  # no successful sync yet
    detail = agent_clients[0].get("/api/workers/worker-a")
    assert detail.status_code == 200 and detail.data["name"] == "worker-a"
    # Staff sessions and other agents may read the registry.
    assert staff_client.get("/api/workers").data["count"] == 1
    assert agent_clients[1].get("/api/workers/worker-a").status_code == 200


def test_worker_read_model_excludes_credential_material(bound_worker):
    bound_worker.last_error = "http://failing:pw@127.0.0.1:4096 unreachable"
    text = str(services.worker_read_model(bound_worker))
    lowered = text.lower()
    assert "password" not in lowered
    assert "api_key" not in lowered and "hash" not in lowered
