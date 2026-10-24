from django.core.management.base import BaseCommand, CommandError

from coordinator import services
from coordinator.models import Agent


class Command(BaseCommand):
    help = (
        "Explicitly provision a worker bridge bound to an existing active agent. "
        "No credential is generated; configure the bridge with that agent's key."
    )

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--agent", required=True)
        parser.add_argument("--opencode-url", default="")
        parser.add_argument("--directory", default="")

    def handle(self, *args, **options):
        name = options["name"]
        agent = Agent.objects.filter(name=options["agent"]).first()
        if agent is None:
            raise CommandError(f"Agent {options['agent']} does not exist")
        try:
            worker = services.provision_worker(name, agent)
        except services.TaskError as exc:
            raise CommandError(exc.message) from exc
        if options["opencode_url"]:
            worker.opencode_url = services.clean_endpoint_url(options["opencode_url"])
        if options["directory"]:
            worker.directory = options["directory"][:500]
        if options["opencode_url"] or options["directory"]:
            worker.save()
        self.stdout.write(f"Worker {worker.name} provisioned and bound to agent {agent.name}.")
        self.stdout.write(
            "Configure the bridge with that agent's Bearer key (shown at provisioning time only)."
        )
