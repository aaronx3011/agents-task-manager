from django.core.management.base import BaseCommand, CommandError

from coordinator import services


class Command(BaseCommand):
    help = (
        "Explicitly deactivate a worker: ingestion and command delivery are blocked "
        "while registry history is preserved. Reactivation is a deliberate action."
    )

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("reason", nargs="?", default="")

    def handle(self, *args, name, reason, **kwargs):
        try:
            worker = services.get_worker(name)
        except services.TaskError as exc:
            raise CommandError(exc.message) from exc
        services.set_worker_enabled(worker, enabled=False, reason=reason)
        self.stdout.write(f"Worker {worker.name} deactivated. {reason}".strip())
