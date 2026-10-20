from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .services import TaskError


def exception_handler(exc, context):
    if isinstance(exc, TaskError):
        return Response({"detail": exc.message}, status=exc.status)
    return drf_exception_handler(exc, context)
