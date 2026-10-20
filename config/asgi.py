import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def build_application():
    http_application = get_asgi_application()
    from channels.auth import AuthMiddlewareStack
    from channels.routing import ProtocolTypeRouter, URLRouter
    from channels.security.websocket import AllowedHostsOriginValidator
    from django.urls import path

    from coordinator.realtime import DashboardConsumer

    return ProtocolTypeRouter(
        {
            "http": http_application,
            "websocket": AllowedHostsOriginValidator(
                AuthMiddlewareStack(
                    URLRouter(
                        [
                            path("ws/dashboard/", DashboardConsumer.as_asgi()),
                        ]
                    )
                )
            ),
        }
    )


application = build_application()
