import json

import pytest
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import Client, override_settings

from coordinator.frontend import dashboard_assets


@pytest.fixture
def vite_build(tmp_path):
    source = tmp_path / "source"
    app = source / "coordinator" / "app"
    (app / "chunks").mkdir(parents=True)
    (app / "assets").mkdir()
    (app / "app-Build123.js").write_text(
        'export const shared = 1; import("./chunks/Panel-Chunk123.js");'
    )
    (app / "chunks" / "Panel-Chunk123.js").write_text(
        'import {shared} from "../app-Build123.js"; export default shared;'
    )
    (app / "assets" / "main-Style123.css").write_text("body { color: green; }")
    (source / "other.css").write_text("body { color: blue; }")
    (app / "manifest.json").write_text(
        json.dumps(
            {
                "src/main.tsx": {
                    "file": "app-Build123.js",
                    "isEntry": True,
                    "css": ["assets/main-Style123.css"],
                }
            }
        )
    )
    with override_settings(
        DEBUG=False,
        STATIC_URL="/static/",
        STATIC_ROOT=tmp_path / "collected",
        STATICFILES_DIRS=[source],
        STATICFILES_FINDERS=["django.contrib.staticfiles.finders.FileSystemFinder"],
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
        },
    ):
        call_command("collectstatic", verbosity=0, interactive=False)
        yield app


@pytest.mark.django_db
def test_dashboard_uses_vite_entry_not_djangos_second_hashed_entry(vite_build, django_user_model):
    client = Client()
    client.force_login(django_user_model.objects.create_user("asset-reader", is_staff=True))
    response = client.get("/dashboard/?section=opencodes")
    html = response.content.decode()
    assert response.status_code == 200
    assert 'src="/static/coordinator/app/app-Build123.js"' in html
    assert 'href="/static/coordinator/app/assets/main-Style123.css"' in html
    second_entry = staticfiles_storage.url("coordinator/app/app-Build123.js")
    assert second_entry != "/static/coordinator/app/app-Build123.js"
    assert second_entry not in html
    assert '/app.js"' not in html
    # Django still versions non-Vite/admin-style static assets normally.
    assert staticfiles_storage.url("other.css") != "/static/other.css"


def test_production_manifest_matches_collected_files_not_newer_source_build(vite_build):
    (vite_build / "manifest.json").write_text(
        json.dumps({"src/main.tsx": {"file": "app-NotCollected.js", "css": []}})
    )
    assets = dashboard_assets()
    assert assets["script"] == "coordinator/app/app-Build123.js"
    with staticfiles_storage.open("coordinator/app/chunks/Panel-Chunk123.js") as source:
        assert b'from "../app-Build123.js"' in source.read()
    with override_settings(DEBUG=True):
        assert dashboard_assets()["script"] == "coordinator/app/app-NotCollected.js"
