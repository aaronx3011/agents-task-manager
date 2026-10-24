from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .services import TaskError


def exception_handler(exc, context):
    if isinstance(exc, TaskError):
        payload = exc.message if isinstance(exc.message, dict) else {"detail": exc.message}
        return Response(payload, status=exc.status)
    return drf_exception_handler(exc, context)
