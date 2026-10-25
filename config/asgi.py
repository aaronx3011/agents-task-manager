import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def build_application():
    http_application = get_asgi_application()
    from channels.auth import AuthMiddlewareStack
    from channels.routing import ProtocolTypeRouter, URLRouter
    from channels.security.websocket import AllowedHostsOriginValidator
    from django.urls import path

    from coordinator.live import WorkerLiveConsumer
    from coordinator.realtime import DashboardConsumer

    return ProtocolTypeRouter(
        {
            "http": http_application,
            "websocket": URLRouter(
                [
                    # Machine clients have no browser Origin or session cookies.
                    path("ws/worker/", WorkerLiveConsumer.as_asgi()),
                    path(
                        "ws/dashboard/",
                        AllowedHostsOriginValidator(
                            AuthMiddlewareStack(DashboardConsumer.as_asgi())
                        ),
                    ),
                ]
            ),
        }
    )


application = build_application()
