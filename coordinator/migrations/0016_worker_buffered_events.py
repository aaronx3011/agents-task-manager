from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("coordinator", "0014_notionlink"),
    ]

    operations = [
        migrations.AddField(
            model_name="worker",
            name="buffered_events",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
