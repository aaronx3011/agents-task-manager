from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import (
    Agent,
    Project,
    TaskEvent,
    Worker,
    WorkerCommand,
    WorkerEvent,
    WorkerMessage,
    WorkerSession,
)
from .realtime import dispatch_notification, publish_change


@receiver(post_save, sender=TaskEvent)
def task_event_committed(sender, instance, created, **kwargs):
    if created:
        event_id = instance.pk
        task_id = instance.task_id
        event_type = instance.event_type

        def notify_dashboard():
            publish_change(event_id, task_id, event_type)

        transaction.on_commit(notify_dashboard, robust=True)


@receiver(post_save, sender=WorkerEvent)
def worker_event_committed(sender, instance, created, **kwargs):
    if created:
        event_id = instance.pk
        worker_id = instance.worker_id
        event_type = instance.event_type

        def notify_dashboard():
            dispatch_notification(
                event_type,
                subject="workers",
                worker=Worker(pk=worker_id),
                event_id=event_id,
            )

        transaction.on_commit(notify_dashboard, robust=True)


@receiver(post_save, sender=WorkerSession)
def session_message_committed(sender, instance, created, **kwargs):
    """Session snapshot changes invalidate the client refetch surface (#15)."""
    event_type = "session.created" if created else "session.changed"
    worker_id = instance.worker_id
    session_id = instance.pk

    def notify_dashboard():
        dispatch_notification(
            event_type,
            subject="sessions",
            worker=Worker(pk=worker_id),
            session=WorkerSession(pk=session_id),
        )

    transaction.on_commit(notify_dashboard, robust=True)


@receiver(post_save, sender=WorkerMessage)
def worker_message_committed(sender, instance, created, **kwargs):
    """Message upserts invalidated via the session identity their refetch uses."""
    event_type = "session.message.created" if created else "session.message.updated"
    worker_id = instance.session.worker_id
    session_id = instance.session_id

    def notify_dashboard():
        dispatch_notification(
            event_type,
            subject="messages",
            worker=Worker(pk=worker_id),
            session=WorkerSession(pk=session_id),
        )

    transaction.on_commit(notify_dashboard, robust=True)


@receiver(post_save, sender=WorkerCommand)
def worker_command_committed(sender, instance, created, **kwargs):
    """Command queue/results state pushes for liveClients (#12/#15)."""
    event_type = "command.queued" if created else "command." + instance.status
    worker_id = instance.worker_id

    def notify_dashboard():
        dispatch_notification(
            event_type,
            subject="commands",
            worker=Worker(pk=worker_id),
            command=instance,
        )

    transaction.on_commit(notify_dashboard, robust=True)


@receiver(post_save, sender=Project)
@receiver(post_delete, sender=Project)
@receiver(post_save, sender=Agent)
@receiver(post_delete, sender=Agent)
@receiver(post_save, sender=Worker)
@receiver(post_delete, sender=Worker)
def directory_changed(sender, **kwargs):
    if sender is Worker:
        worker = kwargs["instance"]
        transaction.on_commit(
            lambda: dispatch_notification("worker.changed", subject="workers", worker=worker),
            robust=True,
        )
        return
    transaction.on_commit(publish_change, robust=True)
