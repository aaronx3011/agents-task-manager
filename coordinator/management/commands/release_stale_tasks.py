from django.core.management.base import BaseCommand, CommandError

from coordinator.services import release_stale_tasks


class Command(BaseCommand):
    help = "Release in-progress tasks with no recent heartbeat."

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=15)

    def handle(self, *args, minutes, **kwargs):
        if minutes < 1:
            raise CommandError("--minutes must be at least 1")
        count = release_stale_tasks(minutes)
        self.stdout.write(self.style.SUCCESS(f"Released {count} stale task(s)."))
