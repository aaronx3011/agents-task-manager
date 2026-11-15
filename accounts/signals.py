from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .services import ensure_profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, raw=False, **kwargs):
    # createsuperuser, admin "add user" and fixtures all get an audited initial role.
    if created and not raw:
        ensure_profile(instance, actor="system", reason="account created")
