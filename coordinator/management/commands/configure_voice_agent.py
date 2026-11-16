import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from coordinator.voice import provider_request
from coordinator.voice_policy import POLICY_VERSION, PROMPT, tool_configs


class Command(BaseCommand):
    help = "Explicitly create/update the dedicated ElevenLabs Task Manager PM agent"

    def add_arguments(self, parser):
        parser.add_argument("--agent-id", help="Update this dedicated agent; otherwise create one")
        parser.add_argument(
            "--tool-id",
            action="append",
            help="Reuse a previously created tool ID; repeat in schema order",
        )

    def handle(self, *args, **options):
        if not settings.VOICE_PUBLIC_ORIGIN.startswith("https://"):
            raise CommandError("VOICE_PUBLIC_ORIGIN must be the public HTTPS Task Manager origin")
        tools = options.get("tool_id") or []
        if not tools:
            for config in tool_configs(settings.VOICE_PUBLIC_ORIGIN):
                result = provider_request("POST", "tools", {"tool_config": config})
                tools.append(result["id"])
                self.stdout.write(f"Configured tool {config['name']}: {result['id']}")
        if len(tools) != len(tool_configs(settings.VOICE_PUBLIC_ORIGIN)):
            raise CommandError("Expected one tool ID for each PM tool")
        payload = {
            "name": "Task Manager PM",
            "tags": ["agents-task-manager", POLICY_VERSION],
            "conversation_config": {
                "agent": {
                    "first_message": "Hi. What would you like to work on?",
                    "language": "en",
                    "prompt": {
                        "prompt": PROMPT,
                        "llm": settings.ELEVENLABS_LLM,
                        "temperature": 0.2,
                        "tool_ids": tools,
                    },
                    "dynamic_variables": {
                        "dynamic_variable_placeholders": {"secret__atm_voice_token": ""}
                    },
                },
                "tts": {"model_id": "eleven_flash_v2"},
                "conversation": {
                    "max_duration_seconds": 1800,
                    "client_events": [
                        "audio",
                        "interruption",
                        "user_transcript",
                        "agent_response",
                        "agent_response_correction",
                        "agent_tool_request",
                        "agent_tool_response",
                        "agent_chat_response_part",
                    ],
                },
            },
            "platform_settings": {
                "auth": {"enable_auth": True},
                "overrides": {
                    "conversation_config_override": {"conversation": {"text_only": True}}
                },
                "privacy": {"record_voice": False, "retention_days": 7},
            },
        }
        if options["agent_id"]:
            agent_id = options["agent_id"]
            provider_request("PATCH", f"agents/{agent_id}", payload)
        else:
            agent_id = provider_request("POST", "agents/create", payload)["agent_id"]
        self.stdout.write(json.dumps({"agent_id": agent_id, "policy_version": POLICY_VERSION}))
        self.stdout.write(
            "Set ELEVENLABS_AGENT_ID in the private backend environment and restart Django."
        )
