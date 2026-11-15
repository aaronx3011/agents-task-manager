"""One-way coordinator → Notion mirror (GH #94).

The coordinator is authoritative. A pass pushes every project whose payload changed
and every task whose ``updated_at`` moved past what was last pushed. Every task
mutation in ``services.py`` saves the task row inside its transaction, so the
committed ``updated_at`` is the change marker: nothing is missed and an
uncommitted change is never mirrored. Notion failures are recorded on the link
with a backoff and never touch coordinator data.

Human-owned Notion properties (Owner, Due, Timeline) and formula/rollup columns
are never written: page updates only send the properties listed here.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from django.conf import settings
from django.db.models import Count, F, Q
from django.utils import timezone

from .models import NotionLink, Project, Task
from .notion import NotionClient, NotionError, encode, heading, paragraphs, plain_text

log = logging.getLogger("coordinator.notion")

PROJECT_PROPERTIES = {"Name", "Slug", "Kind", "Repo", "Docs"}
TASK_PROPERTIES = {
    "Name",
    "Task ID",
    "Status",
    "Priority",
    "Claimed by",
    "Blocked reason",
    "Result",
    "GitHub issue",
    "Claimed at",
    "Created",
    "Depends on",
    "Last heartbeat",
    "Updated",
}
# Pushed on a throttle so heartbeats do not exhaust the Notion request budget.
THROTTLED = {"Last heartbeat", "Updated"}
PROJECT_RELATION = "project relation"  # resolved from the schema by target data source
GITHUB_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+")
TASK_BATCH = 200
MAX_BACKOFF = timedelta(hours=1)


class NotionSyncConfigError(Exception):
    pass


def configured():
    return bool(
        settings.NOTION_TOKEN
        and settings.NOTION_PROJECTS_DATA_SOURCE_ID
        and settings.NOTION_TASKS_DATA_SOURCE_ID
    )


def _norm(uuid_text):
    return (uuid_text or "").replace("-", "").lower()


def _iso(value):
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def github_url(description):
    match = GITHUB_URL.search(description or "")
    return match.group(0) if match else None


@dataclass
class Schema:
    types: dict
    names: dict = field(default_factory=dict)  # logical name -> Notion property name

    @classmethod
    def load(cls, client, data_source_id, required, relation_target=None):
        data = client.data_source(data_source_id)
        properties = data.get("properties", {})
        schema = cls({name: prop.get("type") for name, prop in properties.items()})
        missing = sorted(name for name in required if name not in schema.types)
        if missing:
            raise NotionSyncConfigError(
                f"Notion data source {data_source_id} lacks properties: {', '.join(missing)}"
            )
        schema.names = {name: name for name in required}
        if relation_target:
            matches = [
                name
                for name, prop in properties.items()
                if prop.get("type") == "relation"
                and _norm((prop.get("relation") or {}).get("data_source_id"))
                == _norm(relation_target)
            ]
            if len(matches) != 1:
                raise NotionSyncConfigError(
                    "Tasks data source needs exactly one relation to the Projects data source"
                )
            schema.names[PROJECT_RELATION] = matches[0]
        return schema

    def encode(self, values):
        out = {}
        for logical, value in values.items():
            name = self.names[logical]
            out[name] = encode(self.types[name], value)
        return out


@dataclass
class Stats:
    projects_created: int = 0
    projects_updated: int = 0
    tasks_created: int = 0
    tasks_updated: int = 0
    heartbeats: int = 0
    errors: int = 0

    def changed(self):
        return any(
            [
                self.projects_created,
                self.projects_updated,
                self.tasks_created,
                self.tasks_updated,
                self.heartbeats,
                self.errors,
            ]
        )

    def __str__(self):
        return (
            f"projects +{self.projects_created}/~{self.projects_updated}, "
            f"tasks +{self.tasks_created}/~{self.tasks_updated}, "
            f"heartbeats {self.heartbeats}, errors {self.errors}"
        )


def project_values(project):
    return {
        "Name": project.name,
        "Slug": project.slug,
        "Kind": project.kind,
        "Repo": project.repo_url or None,
        "Docs": project.docs_path or None,
    }


def task_values(task, project_page, dependency_pages):
    return {
        "Name": task.title,
        "Task ID": task.pk,
        PROJECT_RELATION: [project_page],
        "Status": Task.Status(task.status).label,
        "Priority": task.priority,
        "Claimed by": task.claimed_by.name if task.claimed_by_id else None,
        "Blocked reason": task.blocked_reason,
        "Result": task.result_summary,
        "GitHub issue": github_url(task.description),
        "Claimed at": _iso(task.claimed_at),
        "Created": _iso(task.created_at),
        "Depends on": dependency_pages,
    }


def throttled_values(task):
    return {"Last heartbeat": _iso(task.heartbeat_at), "Updated": _iso(task.updated_at)}


def task_body(task):
    blocks = []
    for title, text in (
        ("Description", task.description),
        ("Blocked reason", task.blocked_reason),
        ("Result", task.result_summary),
    ):
        if text and text.strip():
            blocks.append(heading(title))
            blocks.extend(paragraphs(text))
    return blocks


class NotionSync:
    def __init__(self, client=None, *, now=timezone.now):
        if client is None:
            if not configured():
                raise NotionSyncConfigError("Notion sync is not configured")
            client = NotionClient(settings.NOTION_TOKEN)
        self.client = client
        self.now = now
        self.projects_ds = settings.NOTION_PROJECTS_DATA_SOURCE_ID
        self.tasks_ds = settings.NOTION_TASKS_DATA_SOURCE_ID
        self._project_schema = None
        self._task_schema = None
        self._project_index = None
        self._task_index = None

    # --- schema and existing-row indexes ----------------------------------

    @property
    def project_schema(self):
        if self._project_schema is None:
            self._project_schema = Schema.load(self.client, self.projects_ds, PROJECT_PROPERTIES)
        return self._project_schema

    @property
    def task_schema(self):
        if self._task_schema is None:
            self._task_schema = Schema.load(
                self.client, self.tasks_ds, TASK_PROPERTIES, relation_target=self.projects_ds
            )
        return self._task_schema

    def _pages(self, data_source_id, key):
        index = {}
        for page in self.client.query_all(data_source_id):
            if page.get("in_trash") or page.get("archived"):
                continue
            value = plain_text(page.get("properties", {}).get(key))
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if value not in (None, ""):
                index.setdefault(value, []).append(page)
        return index

    def project_index(self):
        if self._project_index is None:
            self._project_index = {
                slug: pages[0]["id"]
                for slug, pages in self._pages(self.projects_ds, "Slug").items()
            }
        return self._project_index

    def task_index(self):
        if self._task_index is None:
            self._task_index = {
                task_id: pages[0]["id"]
                for task_id, pages in self._pages(self.tasks_ds, "Task ID").items()
            }
        return self._task_index

    # --- bookkeeping ------------------------------------------------------

    def _fail(self, link, exc, stats):
        stats.errors += 1
        link.failures += 1
        link.last_error = str(exc)[:2000]
        delay = min(timedelta(seconds=30 * 2 ** min(link.failures - 1, 10)), MAX_BACKOFF)
        link.retry_at = self.now() + delay
        if exc.status == 404 and link.page_id:
            # The page was deleted in Notion: adopt or recreate it on the next pass.
            link.page_id = link.props_hash = link.body_hash = ""
            self._project_index = self._task_index = None
        link.save()
        log.warning("Notion sync failed for %s: %s", link, exc)

    def _ok(self, link):
        link.failures = 0
        link.last_error = ""
        link.retry_at = None
        link.synced_at = self.now()

    def _ready(self, link):
        return link.retry_at is None or link.retry_at <= self.now()

    # --- projects ---------------------------------------------------------

    def sync_projects(self, stats, force=False):
        schema = self.project_schema
        for project in Project.objects.select_related("notion_link").order_by("pk"):
            link = getattr(project, "notion_link", None) or NotionLink(project=project)
            if not self._ready(link) and not force:
                continue
            values = project_values(project)
            digest = _digest(values)
            if link.page_id and link.props_hash == digest and not force:
                continue
            try:
                if not link.page_id:
                    link.page_id = self.project_index().get(project.slug, "")
                if link.page_id:
                    self.client.update_page(link.page_id, schema.encode(values))
                    stats.projects_updated += 1
                else:
                    page = self.client.create_page(self.projects_ds, schema.encode(values))
                    link.page_id = page["id"]
                    self.project_index()[project.slug] = link.page_id
                    stats.projects_created += 1
            except NotionError as exc:
                if exc.status == 401:
                    raise
                self._fail(link, exc, stats)
                continue
            link.props_hash = digest
            self._ok(link)
            link.save()

    # --- tasks ------------------------------------------------------------

    def due_tasks(self, force=False):
        tasks = Task.objects.select_related("project", "claimed_by", "notion_link")
        if not force:
            tasks = tasks.filter(
                Q(notion_link__isnull=True)
                | Q(notion_link__source_updated_at__isnull=True)
                | Q(updated_at__gt=F("notion_link__source_updated_at"))
            ).filter(
                Q(notion_link__isnull=True)
                | Q(notion_link__retry_at__isnull=True)
                | Q(notion_link__retry_at__lte=self.now())
            )
        return tasks.prefetch_related("depends_on").order_by("pk")

    def sync_tasks(self, stats, force=False, limit=TASK_BATCH):
        schema = self.task_schema
        project_pages = dict(
            NotionLink.objects.filter(project__isnull=False)
            .exclude(page_id="")
            .values_list("project_id", "page_id")
        )
        task_pages = dict(
            NotionLink.objects.filter(task__isnull=False)
            .exclude(page_id="")
            .values_list("task_id", "page_id")
        )
        processed = 0
        if force:
            limit = None
        for task in self.due_tasks(force).iterator(chunk_size=100):
            if limit and processed >= limit:
                break
            processed += 1
            link = getattr(task, "notion_link", None) or NotionLink(task=task)
            project_page = project_pages.get(task.project_id)
            if not project_page:
                # Its project failed to sync this pass; retry once the project links.
                continue
            dependency_ids = sorted(dep.pk for dep in task.depends_on.all())
            dependency_pages = [task_pages[pk] for pk in dependency_ids if pk in task_pages]
            complete = len(dependency_pages) == len(dependency_ids)
            values = task_values(task, project_page, dependency_pages)
            digest = _digest(values)
            blocks = task_body(task)
            body_digest = _digest(blocks)
            throttle = throttled_values(task)
            try:
                if not link.page_id:
                    link.page_id = self.task_index().get(task.pk, "")
                    if link.page_id:
                        # Adopted backfill rows already carry this body layout.
                        link.body_hash = body_digest
                if not link.page_id:
                    page = self.client.create_page(
                        self.tasks_ds, schema.encode(values | throttle), blocks
                    )
                    link.page_id = page["id"]
                    self.task_index()[task.pk] = link.page_id
                    link.props_hash, link.body_hash = digest, body_digest
                    link.heartbeat_pushed = task.heartbeat_at
                    stats.tasks_created += 1
                else:
                    pushed = False
                    if force or link.props_hash != digest:
                        self.client.update_page(link.page_id, schema.encode(values | throttle))
                        link.props_hash = digest
                        link.heartbeat_pushed = task.heartbeat_at
                        pushed = True
                    if force or link.body_hash != body_digest:
                        self.client.replace_body(link.page_id, blocks)
                        link.body_hash = body_digest
                        pushed = True
                    if pushed:
                        stats.tasks_updated += 1
                    elif link.heartbeat_pushed != task.heartbeat_at:
                        gap = timedelta(seconds=settings.NOTION_HEARTBEAT_PUSH_SECONDS)
                        if link.synced_at and self.now() - link.synced_at < gap:
                            # Deferred: leave the task due so a later pass pushes it.
                            complete = False
                        else:
                            self.client.update_page(link.page_id, schema.encode(throttle))
                            link.heartbeat_pushed = task.heartbeat_at
                            stats.heartbeats += 1
                            pushed = True
                    if not pushed and complete:
                        link.source_updated_at = task.updated_at
                        link.save()
                        continue
                    if not pushed:
                        link.save()
                        continue
            except NotionError as exc:
                if exc.status == 401:
                    raise
                self._fail(link, exc, stats)
                continue
            task_pages[task.pk] = link.page_id
            if complete:
                link.source_updated_at = task.updated_at
            self._ok(link)
            link.save()

    def run_once(self, force=False):
        stats = Stats()
        self.sync_projects(stats, force=force)
        # Several bounded passes so dependencies created later in the same run link up.
        for _ in range(3):
            before = (stats.tasks_created, stats.tasks_updated, stats.heartbeats, stats.errors)
            self.sync_tasks(stats, force=force)
            force = False
            if before == (stats.tasks_created, stats.tasks_updated, stats.heartbeats, stats.errors):
                break
        return stats

    # --- reconcile --------------------------------------------------------

    def reconcile(self):
        """Compare Notion with the coordinator and requeue anything that drifted."""
        report = {"duplicates": {}, "missing": [], "orphans": [], "status_mismatches": []}
        self._project_index = self._task_index = None
        projects = self._pages(self.projects_ds, "Slug")
        tasks = self._pages(self.tasks_ds, "Task ID")
        relation = self.task_schema.names[PROJECT_RELATION]

        report["duplicates"]["projects"] = sorted(s for s, p in projects.items() if len(p) > 1)
        report["duplicates"]["tasks"] = sorted(t for t, p in tasks.items() if len(p) > 1)
        slugs = set(Project.objects.values_list("slug", flat=True))
        report["missing"] += [f"project {slug}" for slug in sorted(slugs - set(projects))]
        report["orphans"] += [f"project {slug}" for slug in sorted(set(projects) - slugs)]

        coordinator = {
            row["pk"]: row
            for row in Task.objects.values("pk", "status", "project_id", "notion_link__page_id")
        }
        project_pages = dict(
            NotionLink.objects.filter(project__isnull=False).values_list("page_id", "project_id")
        )
        requeue = set()
        for task_id, row in coordinator.items():
            pages = tasks.get(task_id)
            if not pages:
                report["missing"].append(f"task {task_id}")
                requeue.add(task_id)
                continue
            props = pages[0].get("properties", {})
            status = ((props.get("Status") or {}).get("status") or {}).get("name")
            related = [r.get("id") for r in (props.get(relation) or {}).get("relation", [])]
            related_projects = {project_pages.get(page_id) for page_id in related}
            if status != Task.Status(row["status"]).label or related_projects != {
                row["project_id"]
            }:
                report["status_mismatches"].append(task_id)
                requeue.add(task_id)
        report["orphans"] += [
            f"task {task_id}" for task_id in sorted(set(tasks) - set(coordinator), key=str)
        ]

        if requeue:
            links = NotionLink.objects.filter(task_id__in=requeue)
            missing = {task_id for task_id in requeue if not tasks.get(task_id)}
            links.filter(task_id__in=missing).update(page_id="", props_hash="", body_hash="")
            links.update(props_hash="", source_updated_at=None, retry_at=None)

        counts = {}
        for row in Task.objects.values("project__slug", "status").annotate(n=Count("pk")):
            counts.setdefault(row["project__slug"], {})[row["status"]] = row["n"]
        report["coordinator_counts"] = counts
        report["notion_tasks"] = sum(len(pages) for pages in tasks.values())
        report["coordinator_tasks"] = len(coordinator)
        report["requeued"] = len(requeue)
        return report
