"""Give every existing task a card on its project's planning board.

Before this, only tasks launched from the planning queue had a card, so tasks
created through MCP/API/admin were invisible in the project view.
"""

from django.db import migrations

DEFAULT_COLUMNS = ["Ideas", "Backlog", "Ready", "In progress", "Completed"]


def column_for(columns, task, ready):
    if task.status == "pending":
        candidates = ["Ready", "Backlog"] if ready else ["Backlog", "Ready"]
    elif task.status in {"done", "cancelled"}:
        candidates = ["Completed"]
    else:
        candidates = ["In progress"]
    return next((c for c in candidates if c in columns), columns[0])


def backfill(apps, schema_editor):
    Task = apps.get_model("coordinator", "Task")
    ProjectPlan = apps.get_model("coordinator", "ProjectPlan")
    PlanningCard = apps.get_model("coordinator", "PlanningCard")
    linked = PlanningCard.objects.exclude(task=None).values_list("task_id", flat=True)
    tasks = Task.objects.exclude(pk__in=linked).order_by("project_id", "pk")
    plans = {}
    positions = {}
    for task in tasks.iterator():
        plan = plans.get(task.project_id)
        if plan is None:
            plan, _ = ProjectPlan.objects.get_or_create(
                project_id=task.project_id, defaults={"columns": DEFAULT_COLUMNS}
            )
            plans[task.project_id] = plan
        ready = task.status == "pending" and not (task.depends_on.exclude(status="done").exists())
        column = column_for(plan.columns, task, ready)
        key = (plan.pk, column)
        if key not in positions:
            positions[key] = PlanningCard.objects.filter(
                plan=plan, column=column, archived=False
            ).count()
        PlanningCard.objects.create(
            plan=plan,
            task=task,
            title=task.title,
            description=task.description,
            column=column,
            position=positions[key],
        )
        positions[key] += 1


class Migration(migrations.Migration):
    dependencies = [("coordinator", "0017_voice_conversations")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
