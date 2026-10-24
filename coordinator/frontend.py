"""Resolve the Vite-owned module graph without Django rehashing its entry URL."""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage

MANIFEST = "coordinator/app/manifest.json"


def dashboard_assets():
    # Use the same build that WhiteNoise serves in production. DEBUG development
    # can use freshly built source assets without running collectstatic.
    if not settings.DEBUG and staticfiles_storage.exists(MANIFEST):
        with staticfiles_storage.open(MANIFEST) as source:
            manifest = json.load(source)
    else:
        path = finders.find(MANIFEST)
        if not path:
            return None
        manifest = json.loads(Path(path).read_text())
    entry = manifest["src/main.tsx"]
    return {
        "script": "coordinator/app/" + entry["file"],
        "styles": ["coordinator/app/" + name for name in entry.get("css", [])],
    }
