"""Start the loopback-only watchable dashboard demo with disposable test data."""

import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    existing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=atm-dashboard-demo"],
        check=True,
        capture_output=True,
        text=True,
    )
    if existing.stdout.strip():
        raise SystemExit(
            "The demo already exists. Open http://localhost:8000/dashboard/ or remove it first: "
            "docker compose -p atm-dashboard-demo -f compose.demo.yaml down"
        )
    environment = {
        **os.environ,
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
    }
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "atm-dashboard-demo",
            "-f",
            "compose.demo.yaml",
            "up",
            "--build",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    print("Dashboard: http://localhost:8000/dashboard/")
    print("Demo login: demo / " + environment.get("DEMO_ADMIN_PASSWORD", "demo-dashboard"))
    print("Eight scripted MCP agents will keep working in repeating batches.")
    print("Stop: docker compose -p atm-dashboard-demo -f compose.demo.yaml down")


if __name__ == "__main__":
    main()
