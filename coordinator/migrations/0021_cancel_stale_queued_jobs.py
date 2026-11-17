from django.db import migrations
from django.db.models import F
from django.utils import timezone


def invalidate_cancelled_tasks(apps, schema_editor):
    Job = apps.get_model("coordinator", "PlanningJob")
    Plan = apps.get_model("coordinator", "ProjectPlan")
    Event = apps.get_model("coordinator", "TaskEvent")
    alias = schema_editor.connection.alias
    jobs = Job.objects.using(alias).filter(status="queued", card__task__status="cancelled")
    for job in jobs.select_related("card").iterator():
        detail = f"Task #{job.card.task_id} cancelled; stale queue job #{job.pk} invalidated by migration 0021"
        Job.objects.using(alias).filter(pk=job.pk).update(
            status="cancelled", detail=detail, completed_at=timezone.now()
        )
        Plan.objects.using(alias).filter(pk=job.plan_id).update(revision=F("revision") + 1)
        Event.objects.using(alias).create(
            task_id=job.card.task_id, event_type="note", message=detail
        )


class Migration(migrations.Migration):
    dependencies = [("coordinator", "0020_voiceconversation_busy_until_and_more")]
    operations = [migrations.RunPython(invalidate_cancelled_tasks, migrations.RunPython.noop)]
