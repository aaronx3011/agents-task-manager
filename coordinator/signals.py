from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Agent, Project, TaskEvent
from .realtime import publish_change


@receiver(post_save, sender=TaskEvent)
def task_event_committed(sender, instance, created, **kwargs):
    if created:
        event_id = instance.pk
        task_id = instance.task_id
        event_type = instance.event_type

        def notify_dashboard():
            publish_change(event_id, task_id, event_type)

        transaction.on_commit(notify_dashboard, robust=True)


@receiver(post_save, sender=Project)
@receiver(post_delete, sender=Project)
@receiver(post_save, sender=Agent)
@receiver(post_delete, sender=Agent)
def directory_changed(sender, **kwargs):
    transaction.on_commit(publish_change, robust=True)
