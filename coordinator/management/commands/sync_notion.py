import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from coordinator.notion import NotionError
from coordinator.notion_sync import NotionSync, NotionSyncConfigError, configured


class Command(BaseCommand):
    help = "Mirror coordinator projects and tasks into Notion (one-way, GH #94)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep syncing every interval.")
        parser.add_argument(
            "--interval",
            type=int,
            default=None,
            help="Seconds between passes (default NOTION_SYNC_INTERVAL_SECONDS).",
        )
        parser.add_argument(
            "--force", action="store_true", help="Re-push every project and task once."
        )
        parser.add_argument(
            "--reconcile",
            action="store_true",
            help="Compare Notion with the coordinator, requeue drift, then sync.",
        )

    def handle(self, *args, loop, interval, force, reconcile, **kwargs):
        interval = interval or settings.NOTION_SYNC_INTERVAL_SECONDS
        if interval < 1:
            raise CommandError("--interval must be at least 1")
        if not configured():
            message = (
                "Notion sync disabled: set NOTION_TOKEN, NOTION_PROJECTS_DATA_SOURCE_ID "
                "and NOTION_TASKS_DATA_SOURCE_ID."
            )
            if not loop:
                raise CommandError(message)
            self.stderr.write(message)
            while True:  # Idle instead of crash-looping under `restart: unless-stopped`.
                time.sleep(3600)

        sync = NotionSync()
        try:
            if reconcile:
                report = sync.reconcile()
                self.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str))
            stats = sync.run_once(force=force)
            self.stdout.write(f"Notion sync: {stats}")
        except (NotionSyncConfigError, NotionError) as exc:
            raise CommandError(str(exc)) from exc

        while loop:
            time.sleep(interval)
            close_old_connections()
            try:
                stats = sync.run_once()
            except NotionSyncConfigError as exc:
                raise CommandError(str(exc)) from exc
            except NotionError as exc:
                if exc.status == 401:
                    raise CommandError(f"Notion rejected the token: {exc}") from exc
                self.stderr.write(f"Notion sync pass failed: {exc}")
                continue
            if stats.changed():
                self.stdout.write(f"Notion sync: {stats}")
