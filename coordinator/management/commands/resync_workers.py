from django.core.management.base import BaseCommand

from coordinator import services


class Command(BaseCommand):
    help = "Regenerate the sync epoch so worker bridges republish full snapshots."

    def handle(self, *args, **options):
        epoch = services.regenerate_sync_epoch()
        self.stdout.write(f"Sync epoch regenerated: {epoch}")
