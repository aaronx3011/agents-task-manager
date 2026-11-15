"""Rejoin the deployed Planning history with the human-account migrations.

Keep the original migration name: existing installations have already applied it.
Fresh installations add the same fields, without dropping earlier selections.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("coordinator", "0011_planningcard_agent_planningcard_model_and_more"),
        ("coordinator", "0012_backfill_actor_user"),
    ]

    operations = []
