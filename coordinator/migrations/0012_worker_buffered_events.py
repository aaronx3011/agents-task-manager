from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("coordinator", "0011_planningcard_agent_planningcard_model_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="worker",
            name="buffered_events",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
