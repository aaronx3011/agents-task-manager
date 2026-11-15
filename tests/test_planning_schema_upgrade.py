"""Both deployed migration histories must support new backlog cards (#93)."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "previous",
    ["0011_planningcard_agent_planningcard_model_and_more", "0012_backfill_actor_user"],
)
def test_planning_schema_upgrade_preserves_metadata_and_allows_creation(previous):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("coordinator", "0010_projectplan_planningcard_planningjob")])
        executor = MigrationExecutor(connection)
        executor.migrate([("coordinator", previous)])
        apps = executor.loader.project_state([("coordinator", previous)]).apps
        Project = apps.get_model("coordinator", "Project")
        Plan = apps.get_model("coordinator", "ProjectPlan")
        Card = apps.get_model("coordinator", "PlanningCard")
        Worker = apps.get_model("coordinator", "Worker")
        project = Project.objects.create(slug="upgrade-fixture", name="Upgrade fixture")
        legacy = previous.startswith("0011_planning")
        plan = Plan.objects.create(project=project, **({"agent": "planner"} if legacy else {}))
        card = Card.objects.create(
            plan=plan,
            title="Existing work",
            **(
                {"agent": "builder", "model": {"providerID": "test", "modelID": "old"}}
                if legacy
                else {}
            ),
        )
        worker = Worker.objects.create(
            name="upgrade-worker", **({"agent_catalog": [{"name": "builder"}]} if legacy else {})
        )
        executor = MigrationExecutor(connection)
        executor.migrate(latest)

        from coordinator.models import PlanningCard, ProjectPlan
        from coordinator.models import Worker as CurrentWorker

        saved = PlanningCard.objects.get(pk=card.pk)
        assert saved.title == "Existing work"
        assert saved.agent == ("builder" if legacy else "")
        assert saved.model == ({"providerID": "test", "modelID": "old"} if legacy else None)
        assert ProjectPlan.objects.get(pk=plan.pk).agent == ("planner" if legacy else "")
        assert CurrentWorker.objects.get(pk=worker.pk).agent_catalog == (
            [{"name": "builder"}] if legacy else []
        )
        new_card = PlanningCard.objects.create(plan_id=plan.pk, title="New backlog work")
        new_card.refresh_from_db()
        assert new_card.agent == ""
        assert new_card.model is None
        new_project = Project.objects.create(slug="new-project", name="New project")
        assert ProjectPlan.objects.create(project_id=new_project.pk).agent == ""
        assert CurrentWorker.objects.create(name="new-worker").agent_catalog == []
    finally:
        MigrationExecutor(connection).migrate(latest)
