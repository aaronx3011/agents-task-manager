import os
from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Role
from accounts.services import AccountError, set_role

PASSWORD_ENV = "OWNER_PASSWORD"


class Command(BaseCommand):
    help = (
        "Create an owner account, or promote an existing user to owner. The password is "
        f"prompted for, or read from ${PASSWORD_ENV} with --no-input; it is never printed."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--email", default="")
        parser.add_argument("--no-input", action="store_true", dest="no_input")

    def handle(self, *args, username, email, no_input, **kwargs):
        User = get_user_model()
        username = username.strip()
        if not username:
            raise CommandError("username is required")
        user = User.objects.filter(username=username).first()
        if user is None:
            password = self.read_password(no_input)
            try:
                validate_password(password, User(username=username, email=email))
            except ValidationError as exc:
                raise CommandError("; ".join(exc.messages)) from exc
            with transaction.atomic():
                # Staff keeps the current staff-gated dashboard usable until #43 lands.
                user = User.objects.create_user(username, email, password, is_staff=True)
                self.promote(user)
            self.stdout.write(f"Created owner {username}.")
            return
        self.promote(user)
        self.stdout.write(f"{username} is now an owner (password unchanged).")

    def promote(self, user):
        if not user.is_staff or not user.is_active:
            user.is_staff = user.is_active = True
            user.save(update_fields=["is_staff", "is_active"])
        try:
            set_role(user, Role.OWNER, actor="create_owner", reason="management command")
        except AccountError as exc:
            raise CommandError(exc.message) from exc

    def read_password(self, no_input):
        if no_input:
            password = os.environ.get(PASSWORD_ENV, "")
            if not password:
                raise CommandError(f"--no-input requires ${PASSWORD_ENV}")
            return password
        password = getpass("Password: ")
        if password != getpass("Password (again): "):
            raise CommandError("Passwords do not match")
        return password
