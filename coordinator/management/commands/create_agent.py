from django.core.management.base import BaseCommand, CommandError

from coordinator.models import Agent


class Command(BaseCommand):
    help = "Create an agent and print its raw API key once; --rotate replaces an existing key."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--rotate", action="store_true")

    def handle(self, *args, name, rotate, **kwargs):
        if not name.strip() or len(name) > 100:
            raise CommandError("name must contain 1–100 characters")
        agent, created = Agent.objects.get_or_create(name=name)
        if not created and not rotate:
            raise CommandError("Agent exists; use --rotate to replace its API key")
        raw = agent.set_new_api_key()
        agent.save()
        self.stdout.write(f"{agent.name} API key (shown once): {raw}")
