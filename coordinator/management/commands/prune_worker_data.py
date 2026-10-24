from django.core.management.base import BaseCommand

from coordinator import services


class Command(BaseCommand):
    help = (
        "Apply bounded central observation retention (#9): newest messages per "
        "session, newest sessions per worker (audit-linked rows protected), and "
        "the per-worker event ring. Explicit gaps, not a full-archive claim."
    )

    def add_arguments(self, parser):
        parser.add_argument("--verbose", action="store_true")

    def handle(self, *args, verbose, **kwargs):
        removed = services.apply_observation_retention()
        if verbose:
            for name, count in removed.items():
                self.stdout.write(f"{name}: {count} removed")
        self.stdout.write("Retention applied.")
