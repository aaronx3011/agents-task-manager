import time
from functools import wraps

from django.db import OperationalError, connection

from .errors import TaskError


def retry_sqlite(function):
    """Retry a rolled-back deferred SQLite transaction; PostgreSQL uses row locks."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        for attempt in range(6):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                    raise
                if attempt == 5:
                    raise TaskError("Database busy; retry the request", 503) from exc
                time.sleep(0.02 * (2**attempt))

    return wrapped
