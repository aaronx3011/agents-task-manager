import re

from django.conf import settings
from django.db import migrations

# Usernames allow letters, digits and @.+-_ (no colon), so the label ends at ": ".
HUMAN_EVENT = re.compile(r"^Human (?:staff|admin) ([\w.@+-]+): ")
BATCH = 500


def backfill(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    users = dict(User.objects.values_list("username", "pk"))
    if not users:
        return
    TaskEvent = apps.get_model("coordinator", "TaskEvent")
    WorkerCommand = apps.get_model("coordinator", "WorkerCommand")
    PlanningJob = apps.get_model("coordinator", "PlanningJob")
    Agent = apps.get_model("coordinator", "Agent")

    pending = []
    events = TaskEvent.objects.filter(actor_user__isnull=True, message__startswith="Human ")
    for event in events.only("pk", "message").iterator(chunk_size=BATCH):
        match = HUMAN_EVENT.match(event.message)
        if match and match.group(1) in users:
            event.actor_user_id = users[match.group(1)]
            pending.append(event)
        if len(pending) >= BATCH:
            TaskEvent.objects.bulk_update(pending, ["actor_user"])
            pending = []
    TaskEvent.objects.bulk_update(pending, ["actor_user"])

    # A command is human when it has no agent, or when a launch recorded a human
    # label that differs from the issuing agent's own name.
    agent_names = dict(Agent.objects.values_list("pk", "name"))
    pending = []
    commands = WorkerCommand.objects.filter(actor_user__isnull=True).exclude(actor_label="")
    for command in commands.only("pk", "actor_label", "created_by_id").iterator(chunk_size=BATCH):
        label = command.actor_label
        human = command.created_by_id is None or agent_names.get(command.created_by_id) != label
        if human and label in users:
            command.actor_user_id = users[label]
            pending.append(command)
        if len(pending) >= BATCH:
            WorkerCommand.objects.bulk_update(pending, ["actor_user"])
            pending = []
    WorkerCommand.objects.bulk_update(pending, ["actor_user"])

    for username, pk in users.items():
        PlanningJob.objects.filter(actor_user__isnull=True, actor=username).update(actor_user_id=pk)


class Migration(migrations.Migration):
    dependencies = [
        ("coordinator", "0011_actor_user"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    # Reversing drops the columns in 0011; nothing to undo here.
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
