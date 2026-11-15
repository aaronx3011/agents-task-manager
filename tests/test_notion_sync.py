"""Outbound Notion mirror (GH #94) against an in-memory fake Notion."""

import itertools
from datetime import timedelta

import httpx
import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from coordinator import services
from coordinator.models import Agent, NotionLink, Project
from coordinator.notion import NotionClient, NotionError
from coordinator.notion_sync import NotionSync, NotionSyncConfigError, Stats

PROJECTS_DS = "da7c9d26-a2e4-822a-990a-87e338ceb0f7"
TASKS_DS = "30ac9d26-a2e4-830b-aca1-07a758bd0c25"
HUMAN_ONLY = {"Owner", "Due", "Timeline"}


def project_schema():
    return {
        "Name": {"type": "title"},
        "Slug": {"type": "rich_text"},
        "Kind": {"type": "select"},
        "Repo": {"type": "url"},
        "Docs": {"type": "url"},
        "Timeline": {"type": "date"},
        "Tasks": {"type": "relation", "relation": {"data_source_id": TASKS_DS}},
        "State": {"type": "formula"},
    }


def task_schema():
    return {
        # The live workspace names the project relation "" (it is the reverse of
        # Projects.Tasks); the sync must find it by its target data source.
        "": {"type": "relation", "relation": {"data_source_id": PROJECTS_DS.replace("-", "")}},
        "Name": {"type": "title"},
        "Task ID": {"type": "number"},
        "Status": {"type": "status"},
        "Priority": {"type": "number"},
        "Claimed by": {"type": "select"},
        "Blocked reason": {"type": "rich_text"},
        "Result": {"type": "rich_text"},
        "GitHub issue": {"type": "url"},
        "Claimed at": {"type": "date"},
        "Created": {"type": "date"},
        "Updated": {"type": "date"},
        "Last heartbeat": {"type": "date"},
        "Depends on": {"type": "relation", "relation": {"data_source_id": TASKS_DS}},
        "Blocks": {"type": "relation", "relation": {"data_source_id": TASKS_DS}},
        "Owner": {"type": "people"},
        "Due": {"type": "date"},
    }


class FakeNotion:
    def __init__(self):
        self.schemas = {PROJECTS_DS: project_schema(), TASKS_DS: task_schema()}
        self.pages = {}  # id -> {"ds", "properties", "body"}
        self.calls = []
        self.fail_next = []
        self._ids = itertools.count(1)

    def _maybe_fail(self):
        if self.fail_next:
            raise self.fail_next.pop(0)

    def data_source(self, ds):
        return {
            "properties": {name: dict(prop, name=name) for name, prop in self.schemas[ds].items()}
        }

    def query_all(self, ds):
        for page_id, page in self.pages.items():
            if page["ds"] == ds:
                yield {"id": page_id, "properties": self._readable(ds, page["properties"])}

    def _readable(self, ds, properties):
        out = {}
        for name, value in properties.items():
            kind = next(iter(value))
            if kind in {"title", "rich_text"}:
                out[name] = {
                    "type": kind,
                    kind: [{"plain_text": part["text"]["content"]} for part in value[kind]],
                }
            elif kind == "number":
                out[name] = {"type": "number", "number": value["number"]}
            else:
                out[name] = {"type": kind, **value}
        return out

    def create_page(self, ds, properties, children=()):
        self._maybe_fail()
        self.calls.append(("create", ds, properties))
        page_id = f"page-{next(self._ids)}"
        self.pages[page_id] = {"ds": ds, "properties": dict(properties), "body": list(children)}
        return {"id": page_id}

    def update_page(self, page_id, properties):
        self._maybe_fail()
        self.calls.append(("update", page_id, properties))
        if page_id not in self.pages:
            raise NotionError("Notion 404: page not found", 404)
        self.pages[page_id]["properties"].update(properties)
        return {"id": page_id}

    def replace_body(self, page_id, children):
        self._maybe_fail()
        self.calls.append(("body", page_id, children))
        self.pages[page_id]["body"] = list(children)

    def add_page(self, ds, properties):
        page_id = f"existing-{next(self._ids)}"
        self.pages[page_id] = {"ds": ds, "properties": properties, "body": []}
        return page_id

    def page_for(self, ds, key, value):
        for page_id, page in self.pages.items():
            if page["ds"] != ds:
                continue
            prop = page["properties"].get(key, {})
            if "number" in prop and prop["number"] == value:
                return page_id, page
            if (
                "rich_text" in prop
                and prop["rich_text"]
                and (prop["rich_text"][0]["text"]["content"] == value)
            ):
                return page_id, page
        raise AssertionError(f"no page with {key}={value}")


@pytest.fixture
def notion(settings):
    settings.NOTION_TOKEN = "secret-test-token"
    settings.NOTION_PROJECTS_DATA_SOURCE_ID = PROJECTS_DS
    settings.NOTION_TASKS_DATA_SOURCE_ID = TASKS_DS
    settings.NOTION_HEARTBEAT_PUSH_SECONDS = 600
    return FakeNotion()


@pytest.fixture
def agent(db):
    agent = Agent(name="opencode-local")
    agent.set_new_api_key()
    agent.save()
    return agent


class Clock:
    def __init__(self):
        self.value = timezone.now()

    def __call__(self):
        return self.value


def make_sync(fake, clock=None):
    return NotionSync(fake, now=clock or timezone.now)


def task_page(fake, task):
    return fake.page_for(TASKS_DS, "Task ID", task.pk)


def test_first_pass_creates_projects_and_tasks(notion, project, agent):
    project.repo_url = "https://github.com/aaronx3011/agents-task-manager"
    project.save()
    task = services.create_task(
        agent,
        project,
        "Mirror tasks",
        description="Spec: https://github.com/aaronx3011/agents-task-manager/issues/94\n\nMore.",
        priority=10,
    )
    services.claim_task(agent, task.pk)

    stats = make_sync(notion).run_once()

    assert (stats.projects_created, stats.tasks_created, stats.errors) == (1, 1, 0)
    project_page_id, project_page = notion.page_for(PROJECTS_DS, "Slug", "coordinator")
    assert project_page["properties"]["Kind"] == {"select": {"name": "code"}}
    assert project_page["properties"]["Docs"] == {"url": None}
    _, page = task_page(notion, task)
    props = page["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "Mirror tasks"
    assert props["Status"] == {"status": {"name": "In progress"}}
    assert props["Claimed by"] == {"select": {"name": "opencode-local"}}
    assert props[""] == {"relation": [{"id": project_page_id}]}
    assert props["GitHub issue"] == {
        "url": "https://github.com/aaronx3011/agents-task-manager/issues/94"
    }
    assert props["Priority"] == {"number": 10}
    assert props["Created"]["date"]["start"].endswith("Z")
    assert "." not in props["Created"]["date"]["start"]
    assert props["Last heartbeat"]["date"] is not None
    assert [block["type"] for block in page["body"]] == ["heading_2", "paragraph", "paragraph"]
    for _, _, properties in [call for call in notion.calls if call[0] != "body"]:
        assert not HUMAN_ONLY & set(properties)


def test_second_pass_is_a_no_op(notion, project, agent):
    services.create_task(agent, project, "One")
    sync = make_sync(notion)
    sync.run_once()
    notion.calls.clear()

    stats = sync.run_once()

    assert notion.calls == []
    assert not stats.changed()


def test_status_change_pushes_only_that_task(notion, project, agent):
    first = services.create_task(agent, project, "First")
    services.create_task(agent, project, "Second")
    sync = make_sync(notion)
    sync.run_once()
    notion.calls.clear()

    services.claim_task(agent, first.pk)
    services.update_status(agent, first.pk, "done", result_summary="Shipped")
    sync.run_once()

    page_id, page = task_page(notion, first)
    assert {call[1] for call in notion.calls} == {page_id}
    assert page["properties"]["Status"] == {"status": {"name": "Done"}}
    assert page["properties"]["Claimed by"] == {"select": None}
    assert page["body"][-1]["paragraph"]["rich_text"][0]["text"]["content"] == "Shipped"


def test_heartbeats_are_throttled(notion, project, agent):
    task = services.create_task(agent, project, "Long job")
    services.claim_task(agent, task.pk)
    clock = Clock()
    sync = make_sync(notion, clock)
    sync.run_once()
    notion.calls.clear()

    services.heartbeat(agent, task.pk)
    sync.run_once()
    assert notion.calls == []

    clock.value += timedelta(minutes=11)
    services.heartbeat(agent, task.pk)
    stats = sync.run_once()

    assert stats.heartbeats == 1
    ((_, _, properties),) = notion.calls
    assert set(properties) == {"Last heartbeat", "Updated"}
    notion.calls.clear()
    sync.run_once()
    assert notion.calls == []


def test_adopts_backfilled_rows_instead_of_duplicating(notion, project, agent):
    task = services.create_task(agent, project, "Backfilled")
    project_page = notion.add_page(
        PROJECTS_DS, {"Slug": {"rich_text": [{"text": {"content": "coordinator"}}]}}
    )
    existing = notion.add_page(TASKS_DS, {"Task ID": {"number": task.pk}})

    stats = make_sync(notion).run_once()

    assert (stats.projects_created, stats.tasks_created) == (0, 0)
    assert NotionLink.objects.get(task=task).page_id == existing
    assert NotionLink.objects.get(project=project).page_id == project_page
    assert not [call for call in notion.calls if call[0] == "create"]
    assert notion.pages[existing]["properties"]["Status"] == {"status": {"name": "Pending"}}


def test_dependency_on_a_newer_task_resolves_in_the_same_run(notion, project, agent):
    dependent = services.create_task(agent, project, "Dependent")
    prerequisite = services.create_task(agent, project, "Prerequisite")
    services.edit_task(None, dependent.pk, {"depends_on": [prerequisite]}, human_actor="staff")

    make_sync(notion).run_once()

    prerequisite_page, _ = task_page(notion, prerequisite)
    _, page = task_page(notion, dependent)
    assert page["properties"]["Depends on"] == {"relation": [{"id": prerequisite_page}]}
    dependent.refresh_from_db()
    assert NotionLink.objects.get(task=dependent).source_updated_at == dependent.updated_at


def test_notion_failure_never_affects_coordinator_and_retries(notion, project, agent):
    task = services.create_task(agent, project, "Flaky")
    clock = Clock()
    sync = make_sync(notion, clock)
    sync.sync_projects(Stats())
    notion.fail_next.append(NotionError("Notion 503 after retries", 503, retryable=True))

    stats = sync.run_once()

    assert stats.errors == 1
    link = NotionLink.objects.get(task=task)
    assert link.failures == 1 and link.retry_at > clock.value and not link.page_id
    task.refresh_from_db()
    assert task.status == "pending"

    sync.run_once()
    assert NotionLink.objects.get(task=task).failures == 1  # still backing off

    clock.value += timedelta(minutes=5)
    sync.run_once()
    link.refresh_from_db()
    assert link.failures == 0 and link.page_id and link.last_error == ""


def test_page_deleted_in_notion_is_recreated(notion, project, agent):
    task = services.create_task(agent, project, "Will be deleted")
    clock = Clock()
    sync = make_sync(notion, clock)
    sync.run_once()
    old_page, _ = task_page(notion, task)
    del notion.pages[old_page]

    services.edit_task(None, task.pk, {"title": "Renamed"}, human_actor="staff")
    sync.run_once()
    assert NotionLink.objects.get(task=task).page_id == ""

    clock.value += timedelta(minutes=5)
    sync.run_once()
    new_page, page = task_page(notion, task)
    assert new_page != old_page
    assert page["properties"]["Name"]["title"][0]["text"]["content"] == "Renamed"


def test_reconcile_requeues_drift(notion, project, agent):
    task = services.create_task(agent, project, "Drifting")
    sync = make_sync(notion)
    sync.run_once()
    page_id, page = task_page(notion, task)
    page["properties"]["Status"] = {"status": {"name": "Done"}}
    notion.add_page(TASKS_DS, {"Task ID": {"number": 9999}})

    report = sync.reconcile()

    assert report["status_mismatches"] == [task.pk]
    assert report["orphans"] == ["task 9999"]
    notion.calls.clear()
    sync.run_once()
    assert notion.pages[page_id]["properties"]["Status"] == {"status": {"name": "Pending"}}


def test_missing_property_stops_the_sync(notion, project):
    del notion.schemas[TASKS_DS]["Result"]
    with pytest.raises(NotionSyncConfigError, match="Result"):
        make_sync(notion).run_once()


def test_project_change_is_pushed(notion, project):
    sync = make_sync(notion)
    sync.run_once()
    Project.objects.filter(pk=project.pk).update(name="Renamed project")
    notion.calls.clear()

    sync.run_once()

    ((kind, _, properties),) = notion.calls
    assert kind == "update"
    assert properties["Name"]["title"][0]["text"]["content"] == "Renamed project"


def test_command_requires_configuration(db, settings):
    settings.NOTION_TOKEN = ""
    with pytest.raises(CommandError, match="disabled"):
        call_command("sync_notion")


def test_client_retries_rate_limits_and_hides_token():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"id": "page"})

    sleeps = []
    http = httpx.Client(
        base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler)
    )
    client = NotionClient("secret-token", http=http, min_interval=0, sleep=sleeps.append)

    assert client.update_page("abc", {}) == {"id": "page"}
    assert sleeps == [2.0]
    assert seen[-1].headers["Notion-Version"]

    def failing(request):
        return httpx.Response(400, json={"message": "validation failed"})

    client = NotionClient(
        "secret-token",
        http=httpx.Client(
            base_url="https://api.notion.com/v1", transport=httpx.MockTransport(failing)
        ),
        min_interval=0,
        sleep=sleeps.append,
    )
    with pytest.raises(NotionError) as exc:
        client.update_page("abc", {})
    assert exc.value.status == 400 and "secret-token" not in str(exc.value)
