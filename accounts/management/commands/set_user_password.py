import os
from getpass import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.services import AccountError, set_password

PASSWORD_ENV = "USER_PASSWORD"


class Command(BaseCommand):
    help = (
        "Recovery: set a user's password without email. Prompts for it, or reads "
        f"${PASSWORD_ENV} with --no-input. The password is never printed; the change "
        "is audited and the user's existing sessions stop working."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--no-input", action="store_true", dest="no_input")

    def handle(self, *args, username, no_input, **kwargs):
        user = get_user_model().objects.filter(username=username.strip()).first()
        if user is None:
            raise CommandError(f"No user named {username!r}")
        if no_input:
            password = os.environ.get(PASSWORD_ENV, "")
            if not password:
                raise CommandError(f"--no-input requires ${PASSWORD_ENV}")
        else:
            password = getpass("New password: ")
            if password != getpass("New password (again): "):
                raise CommandError("Passwords do not match")
        try:
            set_password(user, password, actor="set_user_password")
        except AccountError as exc:
            raise CommandError(exc.message) from exc
        self.stdout.write(f"Password updated for {user.get_username()}.")
