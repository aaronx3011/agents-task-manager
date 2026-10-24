class TaskError(Exception):
    """Domain error rendered as {"detail": message} by the exception handler."""

    def __init__(self, message, status=409):
        self.message = message
        self.status = status
        super().__init__(message)
