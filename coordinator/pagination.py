from rest_framework.exceptions import ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response


class TaskPagination(LimitOffsetPagination):
    default_limit = 100
    max_limit = 500

    def paginate_queryset(self, queryset, request, view=None):
        for name in ["limit", "offset"]:
            if name in request.query_params:
                try:
                    value = int(request.query_params[name])
                except ValueError as exc:
                    raise ValidationError({name: "Must be an integer"}) from exc
                if (name == "limit" and not 1 <= value <= 500) or (name == "offset" and value < 0):
                    raise ValidationError({name: "Invalid pagination value"})
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        return Response({"count": self.count, "items": data})
