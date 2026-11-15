from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("coordinator", "0014_notionlink")]

    operations = [
        migrations.AddField(
            model_name="worker",
            name="codex_usage",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
