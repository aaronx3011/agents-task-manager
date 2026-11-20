"""Apply the prompts/ library to every project's Planning settings.

Standing instructions <- prompts/base.md
Prompt templates      <- prompts/roles/*.md (existing non-library templates are kept)
Worker, directory, agent, model, columns and project context are left unchanged.

Dry run by default; --apply writes. Needs a human member/owner login:
    ATM_USERNAME=... ATM_PASSWORD=... python3 scripts/apply_worker_prompts.py [--apply]
Without ATM_PASSWORD it prompts for the password. Previous settings are backed up to
.runtime/prompt-backups/ before any write.
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
MAX_TEMPLATES = 30


def library():
    base = (PROMPTS / "base.md").read_text().strip()
    roles = []
    for path in sorted((PROMPTS / "roles").glob("*.md")):
        text = path.read_text().strip()
        match = re.match(r"##\s*Role:\s*(.+)", text)
        if not match:
            sys.exit(f"{path.name}: first line must be '## Role: <name>'")
        roles.append({"name": match.group(1).strip()[:100], "text": text})
    for item in [base, *(r["text"] for r in roles)]:
        if len(item) > 8000:
            sys.exit("A prompt exceeds the 8,000-character field limit")
    return base, roles


class Client:
    def __init__(self, origin):
        self.origin = origin.rstrip("/")
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def csrf(self):
        return next((c.value for c in self.jar if c.name == "csrftoken"), "")

    def request(self, method, path, body=None):
        headers = {
            "Accept": "application/json",
            "Origin": self.origin,
            "Referer": self.origin + "/",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["X-CSRFToken"] = self.csrf()
        req = urllib.request.Request(self.origin + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw.decode(errors="replace")[:300]

    def login(self, username, password):
        self.request("GET", "/api/v1/auth/csrf")
        status, body = self.request(
            "POST", "/api/v1/auth/login", {"username": username, "password": password}
        )
        if status != 200:
            sys.exit(f"Login failed ({status}): {body}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", default=os.environ.get("ATM_URL", "https://atm.aaron-henriquez.com")
    )
    parser.add_argument("--project", action="append", help="Limit to these slugs (repeatable)")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()

    base, roles = library()
    role_names = {r["name"] for r in roles}

    username = os.environ.get("ATM_USERNAME") or input("ATM username: ")
    password = os.environ.get("ATM_PASSWORD") or getpass.getpass("ATM password: ")
    client = Client(args.url)
    client.login(username, password)

    status, overview = client.request("GET", "/api/v1/overview")
    if status != 200:
        sys.exit(f"Could not list projects ({status}): {overview}")
    slugs = [p.get("slug") or p.get("project") for p in overview]
    if args.project:
        slugs = [s for s in slugs if s in set(args.project)]

    backup_dir = ROOT / ".runtime" / "prompt-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"settings-{time.strftime('%Y%m%d-%H%M%S')}.json"
    backups, failures = {}, []

    for slug in slugs:
        status, plan = client.request("GET", f"/api/v1/projects/{slug}/planning")
        if status != 200:
            failures.append(slug)
            print(f"✗ {slug}: read failed ({status}) {plan}")
            continue
        current = plan["settings"]
        backups[slug] = current
        kept = [t for t in current["templates"] if t["name"] not in role_names]
        templates = roles + kept
        if len(templates) > MAX_TEMPLATES:
            failures.append(slug)
            print(f"✗ {slug}: {len(templates)} templates exceed the {MAX_TEMPLATES} limit")
            continue
        notes = []
        if current["instructions"].strip() and current["instructions"].strip() != base:
            notes.append(f"replaces {len(current['instructions'])} chars of standing instructions")
        if kept:
            notes.append(f"keeps custom templates: {', '.join(t['name'] for t in kept)}")
        if not current["context"].strip():
            notes.append("project context is empty")
        unchanged = current["instructions"].strip() == base and current["templates"] == templates
        label = "unchanged" if unchanged else ("apply" if args.apply else "would apply")
        print(f"• {slug}: {label}" + (f" — {'; '.join(notes)}" if notes else ""))
        if not args.apply or unchanged:
            continue
        settings = {**current, "instructions": base, "templates": templates}
        status, body = client.request(
            "POST",
            f"/api/v1/projects/{slug}/planning",
            {"revision": plan["revision"], "operation": "settings", "settings": settings},
        )
        if status != 200:
            failures.append(slug)
            print(f"  ✗ write failed ({status}): {body}")
        else:
            print("  ✓ saved")

    backup_file.write_text(json.dumps(backups, indent=2, ensure_ascii=False))
    os.chmod(backup_file, 0o600)
    print(f"\nBackup of previous settings: {backup_file}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to write.")
    if failures:
        sys.exit(f"Failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
