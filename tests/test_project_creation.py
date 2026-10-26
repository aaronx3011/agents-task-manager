from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import Client

from coordinator import services
from coordinator.models import Project, ProjectPlan

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff():
    client = Client()
    client.force_login(User.objects.create_user("project-creator", is_staff=True))
    return client


def test_staff_creation_selectable_in_workspace_and_planning(staff):
    response = staff.post(
        "/api/projects",
        {
            "slug": "my-project",
            "name": " My project ",
            "kind": "design",
            "repo_url": "https://github.com/example/project",
            "docs_path": "docs/project.md",
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["name"] == "My project"
    project = Project.objects.get(slug="my-project")
    assert project.kind == "design"
    assert ProjectPlan.objects.filter(project=project).exists()
    workspace = staff.get("/dashboard/workspace/").json()
    assert any(p["slug"] == project.slug for p in workspace["projects"])
    plan = staff.get("/api/projects/my-project/planning").json()
    assert plan["cards"] == [] and plan["settings"]["worker"] == ""


def test_duplicates_do_not_overwrite_existing_projects(staff, project):
    response = staff.post(
        "/api/projects",
        {"slug": project.slug, "name": "Replace me"},
        content_type="application/json",
    )
    assert response.status_code == 409
    project.refresh_from_db()
    assert project.name == "Coordinator"
    assert not ProjectPlan.objects.filter(project=project).exists()


@pytest.mark.parametrize(
    "values",
    [
        {"slug": "", "name": "Name"},
        {"slug": "bad slug", "name": "Name"},
        {"slug": "x" * 51, "name": "Name"},
        {"slug": "valid", "name": " "},
        {"slug": "valid", "name": "Name", "kind": "invalid"},
        {"slug": "valid", "name": "Name", "repo_url": "not-a-url"},
    ],
)
def test_invalid_input_rejected_without_project_creation(staff, values):
    response = staff.post("/api/projects", values, content_type="application/json")
    assert response.status_code == 400
    assert not Project.objects.exists()


def test_creation_requires_staff_session_and_csrf(agent_clients):
    body = {"slug": "project", "name": "Project"}
    assert Client().post("/api/projects", body, content_type="application/json").status_code == 401
    assert (
        agent_clients[0].post("/api/projects", body, content_type="application/json").status_code
        == 403
    )
    user = User.objects.create_user("staff", is_staff=True)
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post("/api/projects", body, content_type="application/json").status_code == 403
    csrf.get("/dashboard/")
    response = csrf.post(
        "/api/projects",
        body,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf.cookies["csrftoken"].value,
    )
    assert response.status_code == 201
    nonstaff = Client()
    nonstaff.force_login(User.objects.create_user("nonstaff"))
    assert nonstaff.post("/api/projects", body, content_type="application/json").status_code == 403


@pytest.mark.django_db(transaction=True)
def test_concurrent_project_creation_has_one_winner():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL verifies competing slug creation")
    barrier = Barrier(2)

    def create():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            try:
                services.create_project("same-slug", "Project", "code")
                return 201
            except services.TaskError as error:
                return error.status
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: create(), range(2))) == [201, 409]
    assert Project.objects.count() == ProjectPlan.objects.count() == 1
