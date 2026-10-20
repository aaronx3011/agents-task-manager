from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, transaction
from django.test import Client

from coordinator.models import Agent


def create(client, title="Write acceptance criteria", **kwargs):
    response = client.post(
        "/api/tasks",
        {
            "title": title,
            "project": "coordinator",
            **kwargs,
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    return response.json()


def post(client, path, data=None):
    return client.post(f"/api/{path}", data or {}, content_type="application/json")


@pytest.mark.django_db
def test_authentication_required_and_inactive_keys_rejected(api_client, project):
    assert Client().get("/api/tasks").status_code == 401
    assert Client(HTTP_AUTHORIZATION="Bearer invalid").get("/api/overview").status_code == 401
    assert api_client.get("/api/tasks").status_code == 200
    agent = Agent.objects.get(name="alpha")
    assert agent.last_seen is not None
    assert "Bearer" not in agent.api_key_hash
    agent.is_active = False
    agent.save()
    assert api_client.get("/api/tasks").status_code == 401


@pytest.mark.django_db
def test_dependency_readiness_priority_and_audit_history(api_client, project):
    prerequisite = create(api_client, "Prerequisite", priority=50)
    dependent = create(
        api_client, "Urgent but waiting", priority=1, depends_on=[prerequisite["id"]]
    )
    later = create(api_client, "Later", priority=100)
    ready = api_client.get("/api/tasks", {"ready": "true"}).json()
    assert [task["id"] for task in ready["items"]] == [prerequisite["id"], later["id"]]
    assert not api_client.get(f"/api/tasks/{dependent['id']}").json()["is_ready"]
    assert post(api_client, f"tasks/{dependent['id']}/claim").status_code == 409
    claimed = post(api_client, "tasks/next").json()
    assert claimed["id"] == prerequisite["id"]
    assert claimed["claimed_by"] == "alpha"
    assert post(api_client, f"tasks/{claimed['id']}/heartbeat").status_code == 200
    assert (
        post(api_client, f"tasks/{claimed['id']}/notes", {"message": "Verified spec"}).status_code
        == 201
    )
    done = post(
        api_client,
        f"tasks/{claimed['id']}/status",
        {
            "status": "done",
            "result_summary": "Acceptance criteria documented",
        },
    ).json()
    assert done["claimed_by"] is None and done["heartbeat_at"] is None
    assert post(api_client, "tasks/next").json()["id"] == dependent["id"]
    detail = api_client.get(f"/api/tasks/{prerequisite['id']}").json()
    assert [event["event_type"] for event in detail["recent_events"]] == [
        "status_changed",
        "note",
        "heartbeat",
        "claimed",
        "created",
    ]
    assert "api_key_hash" not in str(detail)


@pytest.mark.django_db
def test_owner_enforcement_and_blocked_review_resume_release(agent_clients, project):
    owner, stranger = agent_clients
    task = create(owner)
    task_id = task["id"]
    assert post(owner, f"tasks/{task_id}/claim").status_code == 200
    for action, payload in [
        ("heartbeat", {}),
        ("status", {"status": "done", "result_summary": "Stolen"}),
        ("release", {}),
        ("notes", {"message": "Unowned update"}),
    ]:
        assert post(stranger, f"tasks/{task_id}/{action}", payload).status_code == 403
    assert post(owner, f"tasks/{task_id}/status", {"status": "blocked"}).status_code == 400
    blocked = post(
        owner,
        f"tasks/{task_id}/status",
        {
            "status": "blocked",
            "reason": "Waiting for design sign-off",
        },
    ).json()
    assert blocked["claimed_by"] == "alpha"
    assert post(owner, f"tasks/{task_id}/heartbeat").status_code == 409
    assert post(owner, f"tasks/{task_id}/status", {"status": "in_progress"}).status_code == 200
    assert post(owner, f"tasks/{task_id}/status", {"status": "review"}).status_code == 400
    assert (
        post(
            owner,
            f"tasks/{task_id}/status",
            {
                "status": "review",
                "result_summary": "Ready for human review",
            },
        ).status_code
        == 200
    )
    released = post(owner, f"tasks/{task_id}/release", {"reason": "Handing off"}).json()
    assert released["status"] == "pending" and released["claimed_by"] is None
    assert released["claimed_at"] is None and released["heartbeat_at"] is None
    assert post(stranger, f"tasks/{task_id}/claim").json()["claimed_by"] == "beta"


@pytest.mark.django_db
def test_filters_overview_and_invalid_input(api_client, project):
    first = create(api_client)
    create(api_client, "Dependent", depends_on=[first["id"]])
    assert api_client.get("/api/tasks", {"limit": 1, "offset": 1}).json()["count"] == 2
    assert api_client.get("/api/tasks", {"ready": "false"}).json()["count"] == 1
    assert api_client.get("/api/tasks", {"status": "wrong"}).status_code == 400
    assert api_client.get("/api/tasks", {"limit": 501}).status_code == 400
    assert api_client.get("/api/tasks", {"project": "missing"}).status_code == 404
    assert post(api_client, "tasks/next", {"project": "missing"}).status_code == 400
    for payload in [{"title": " "}, {"depends_on": [999999]}, {"priority": 2**40}]:
        assert (
            post(
                api_client,
                "tasks",
                {
                    "title": "Test",
                    "project": project.slug,
                    **payload,
                },
            ).status_code
            == 400
        )
    post(api_client, f"tasks/{first['id']}/claim")
    summary = api_client.get("/api/overview").json()[0]
    assert summary["counts"]["pending"] == 1
    assert summary["counts"]["done"] == 0
    assert summary["in_progress"][0]["agent"] == "alpha"
    assert summary["ready_count"] == 0
    assert len(summary["waiting"]) == 1
    assert api_client.get("/api/tasks", {"claimed_by": "alpha"}).json()["count"] == 1
    assert api_client.get("/api/tasks/999999").status_code == 404
    assert post(api_client, "tasks/999999/claim").status_code == 404


@pytest.mark.django_db
def test_cancelled_prerequisite_does_not_make_dependent_ready(api_client, project):
    prerequisite = create(api_client)
    dependent = create(api_client, depends_on=[prerequisite["id"]])
    post(api_client, f"tasks/{prerequisite['id']}/claim")
    post(
        api_client,
        f"tasks/{prerequisite['id']}/status",
        {
            "status": "cancelled",
            "reason": "Superseded",
        },
    )
    assert api_client.get("/api/tasks", {"ready": "true"}).json()["count"] == 0
    assert post(api_client, "tasks/next").status_code == 404
    assert not api_client.get(f"/api/tasks/{dependent['id']}").json()["is_ready"]


@pytest.mark.django_db
def test_browsable_api_requires_authentication(client, api_client):
    assert client.get("/api/").status_code == 401
    assert api_client.get("/api/", HTTP_ACCEPT="text/html").status_code == 200


@pytest.mark.django_db
def test_patch_is_owned_audited_and_cannot_bypass_lifecycle(agent_clients, project):
    owner, stranger = agent_clients
    task = create(owner)
    task_id = task["id"]
    url = f"/api/tasks/{task_id}"
    assert (
        owner.patch(url, {"title": "Unclaimed"}, content_type="application/json").status_code == 403
    )
    post(owner, f"tasks/{task_id}/claim")
    assert stranger.patch(url, {"priority": 1}, content_type="application/json").status_code == 403
    changed = owner.patch(
        url,
        {
            "title": "Clarified scope",
            "priority": 5,
            "status": "done",
            "claimed_by": "beta",
        },
        content_type="application/json",
    )
    assert changed.status_code == 200
    assert changed.json()["title"] == "Clarified scope"
    assert changed.json()["status"] == "in_progress"
    assert changed.json()["claimed_by"] == "alpha"
    detail = owner.get(url).json()
    assert detail["recent_events"][0]["event_type"] == "note"
    assert "title" in detail["recent_events"][0]["message"]
    assert owner.put(url, {}, content_type="application/json").status_code == 405
    assert owner.delete(url).status_code == 405


@pytest.mark.django_db
def test_patch_rejects_dependency_cycle_and_rolls_back_all_edits(api_client, project):
    first = create(api_client)
    second = create(api_client, depends_on=[first["id"]])
    post(api_client, f"tasks/{first['id']}/claim")
    response = api_client.patch(
        f"/api/tasks/{first['id']}",
        {
            "title": "Must not persist",
            "depends_on": [second["id"]],
        },
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "cycle" in response.json()["detail"]
    detail = api_client.get(f"/api/tasks/{first['id']}").json()
    assert detail["title"] == first["title"]
    assert detail["depends_on"] == []


@pytest.mark.django_db
def test_staff_session_can_override_with_csrf_and_is_audited(
    django_user_model, api_client, project
):
    user = django_user_model.objects.create_user("staff", is_staff=True)
    staff = Client(enforce_csrf_checks=True)
    staff.force_login(user)
    assert staff.get("/api/tasks").status_code == 200
    task = create(api_client)
    post(api_client, f"tasks/{task['id']}/claim")
    payload = {"status": "done", "result_summary": "Human verified"}
    assert post(staff, f"tasks/{task['id']}/status", payload).status_code == 403
    staff.get("/api/tasks", HTTP_ACCEPT="text/html")
    token = staff.cookies["csrftoken"].value
    response = staff.post(
        f"/api/tasks/{task['id']}/status",
        payload,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    event = api_client.get(f"/api/tasks/{task['id']}").json()["recent_events"][0]
    assert event["agent"] is None
    assert "Human staff staff" in event["message"]
    assert (
        staff.post(
            "/api/tasks/next", {}, content_type="application/json", HTTP_X_CSRFTOKEN=token
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_nonstaff_session_and_malformed_bearer_are_denied(client, django_user_model):
    user = django_user_model.objects.create_user("ordinary")
    client.force_login(user)
    assert client.get("/api/tasks").status_code == 403
    assert Client(HTTP_AUTHORIZATION="Bearer").get("/api/tasks").status_code == 401
    assert Client(HTTP_AUTHORIZATION="Bearer x extra").get("/api/tasks").status_code == 401


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("specific", [False, True])
def test_competing_claims_never_return_the_same_task(agent_clients, project, specific):
    task = create(agent_clients[0])
    barrier = Barrier(2)

    def claim(client):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            path = f"tasks/{task['id']}/claim" if specific else "tasks/next"
            response = post(client, path)
            return response.status_code, response.json()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, agent_clients))
    successes = [body for status, body in results if status == 200 and body is not None]
    assert len(successes) == 1, results
    assert successes[0]["id"] == task["id"]
    assert sorted(status for status, _ in results) == ([200, 409] if specific else [200, 404])
    detail = agent_clients[0].get(f"/api/tasks/{task['id']}").json()
    assert [e["event_type"] for e in detail["recent_events"]].count("claimed") == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_skips_locked_highest_priority_task(agent_clients, project):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock semantics")
    first = create(agent_clients[0], priority=1)
    second = create(agent_clients[0], priority=2)

    def claim():
        close_old_connections()
        try:
            return post(agent_clients[1], "tasks/next").json()
        finally:
            close_old_connections()

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM coordinator_task WHERE id = %s FOR UPDATE", [first["id"]]
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            claimed = pool.submit(claim).result(timeout=10)
        assert claimed["id"] == second["id"]


@pytest.mark.django_db(transaction=True)
def test_postgres_specific_claim_returns_conflict_without_waiting(agent_clients, project):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock semantics")
    task = create(agent_clients[0])

    def claim():
        close_old_connections()
        try:
            return post(agent_clients[1], f"tasks/{task['id']}/claim").status_code
        finally:
            close_old_connections()

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM coordinator_task WHERE id = %s FOR UPDATE",
                [task["id"]],
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(claim).result(timeout=10) == 409


@pytest.mark.django_db(transaction=True)
def test_concurrent_dependency_edits_cannot_create_a_cycle(agent_clients, project):
    first = create(agent_clients[0], "First")
    second = create(agent_clients[1], "Second")
    post(agent_clients[0], f"tasks/{first['id']}/claim")
    post(agent_clients[1], f"tasks/{second['id']}/claim")
    barrier = Barrier(2)

    def edit(args):
        client, target, prerequisite = args
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = client.patch(
                f"/api/tasks/{target['id']}",
                {
                    "depends_on": [prerequisite["id"]],
                },
                content_type="application/json",
            )
            return response.status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                edit,
                [
                    (agent_clients[0], first, second),
                    (agent_clients[1], second, first),
                ],
            )
        )
    assert sorted(results) == [200, 400]
    details = [agent_clients[0].get(f"/api/tasks/{task['id']}").json() for task in [first, second]]
    assert sum(len(task["depends_on"]) for task in details) == 1
