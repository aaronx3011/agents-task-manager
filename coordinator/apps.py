from django.apps import AppConfig


class CoordinatorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "coordinator"

    def ready(self):
        from . import openapi, signals  # noqa: F401  (openapi registers schema extensions)
