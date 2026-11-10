"""Portable exports of the retained observation, never worker filesystem reads."""

import base64
import binascii
import io
import json
import re
import zipfile
from urllib.parse import unquote_to_bytes

from django.db.models import BigIntegerField
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone

from . import services

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 32 * 1024 * 1024


def safe_name(value, fallback="file"):
    # No directory traversal, control characters, or platform-dependent separators.
    name = re.sub(r"[^\w. -]", "_", str(value)).strip(" .")[:100]
    return name or fallback


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def fenced(value):
    text = value if isinstance(value, str) else json_text(value)
    runs = re.findall(r"`+", text)
    fence = "`" * max(3, 1 + max(map(len, runs), default=0))
    return f"{fence}\n{text}\n{fence}"


def embedded_file(url):
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, "File contents were not supplied; reference only."
    if len(url) > MAX_ATTACHMENT_BYTES * 4:
        return None, "Embedded file exceeds the export size limit."
    try:
        header, body = url.split(",", 1)
        data = (
            base64.b64decode(body, validate=True)
            if header.endswith(";base64")
            else unquote_to_bytes(body)
        )
    except (ValueError, binascii.Error):
        return None, "Invalid embedded file encoding."
    if len(data) > MAX_ATTACHMENT_BYTES:
        return None, "Embedded file exceeds the export size limit."
    return data, ""


def export_session(session, kind):
    if kind not in {"md", "zip"}:
        raise services.TaskError("Export kind must be md or zip", 400)
    # A single materialized query includes older retained messages even when the
    # browser has loaded only its latest page. Same timeline as history reads.
    messages = [
        services.message_card(row)
        for row in session.messages.annotate(
            timeline_time=Coalesce("remote_time", 0, output_field=BigIntegerField())
        ).order_by("timeline_time", "pk")
    ]
    metadata = {
        "schemaVersion": 1,
        "exportedAt": timezone.now().isoformat(),
        "session": services.session_card(session),
        "coverage": {
            "source": "retained server observation",
            "fullHistoryGuaranteed": False,
            "suppliedWindowComplete": session.window_complete,
            "messageCount": len(messages),
            "note": "OpenCode owns canonical history. Older/pruned messages and unflushed live "
            "updates may be absent. Related sessions are references, not included transcripts.",
        },
        "attachments": [],
    }
    intro = (
        f"# {session.title or session.remote_id}\n\n"
        f"Worker: {session.worker.name}\n\nSession: {session.remote_id}\n\n"
        f"Exported: {metadata['exportedAt']}\n\n"
        f"Coverage: retained server observation · {len(messages)} messages. "
        "Full history is not guaranteed.\n\n"
        f"{metadata['coverage']['note']}\n\n"
        f"Supplied window complete: {session.window_complete}.\n\n"
        "Messages are ordered by remote creation time, then stored ID; missing times sort first. "
        "Parts retain their original order. "
        "Reasoning sections contain only supplied readable text.\n"
    )
    if session.remote_parent_id:
        intro += f"\nParent session: {session.remote_parent_id}\n"
    children = list(session.subagents.order_by("pk").values_list("remote_id", flat=True))
    if children:
        intro += f"\nRelated sessions: {', '.join(children)}\n"
    files = {}
    sections = []
    attachment_bytes = 0
    for index, message in enumerate(messages, 1):
        info = message["content"] if isinstance(message["content"], dict) else {}
        section = [
            f"## {index:04d} · {message['role'] or 'message'}",
            f"Message: {message['remoteID']} · Remote time (ms): {message['remoteTime']}",
        ]
        if info.get("summary"):
            section.append("### Compaction summary")
        if info.get("error"):
            section.extend(["### Error", fenced(info["error"])])
        for part_index, part in enumerate(message["parts"]):
            if not isinstance(part, dict):
                section.append(fenced(part))
                continue
            part_type = part.get("type", "unknown")
            text = part.get("text")
            if part_type == "text":
                if isinstance(text, str):
                    section.append(text)
            elif part_type == "reasoning":
                if isinstance(text, str) and text.strip():
                    section.extend(["### Reasoning summary (provider supplied)", text])
                else:
                    section.append("_Reasoning part: no readable summary was supplied._")
            elif part_type == "compaction":
                section.extend(["### Compaction marker", fenced(part)])
            elif part_type == "file":
                filename = str(part.get("filename") or "attachment")
                path = f"attachments/{index:04d}-{part_index + 1:03d}-{safe_name(filename)}"
                data, reason = embedded_file(part.get("url"))
                if data is not None and attachment_bytes + len(data) > MAX_TOTAL_ATTACHMENT_BYTES:
                    data, reason = None, "Total embedded attachment export limit reached."
                if data is not None:
                    attachment_bytes += len(data)
                    files[path] = data
                reference = part.get("url")
                if not isinstance(reference, str) or reference.startswith("data:"):
                    reference = None
                attachment = {
                    "messageID": message["remoteID"],
                    "partIndex": part_index,
                    "filename": filename,
                    "mime": part.get("mime"),
                    "path": path if data is not None else None,
                    "reference": reference,
                    "status": "included" if data is not None else "unavailable",
                    "reason": reason,
                }
                metadata["attachments"].append(attachment)
                section.extend(
                    [
                        "### Attachment",
                        fenced({key: value for key, value in attachment.items() if key != "path"}),
                    ]
                )
                if data is not None:
                    section.append(
                        f"[{safe_name(filename)}]({path})"
                        if kind == "zip"
                        else "Embedded file contents are available in the ZIP export."
                    )
            elif part_type == "tool":
                section.extend([f"### Tool: {part.get('tool', 'unknown')}", fenced(part)])
            else:
                section.extend([f"### Part: {part_type}", fenced(part)])
        if not message["parts"]:
            section.extend(["### Message content", fenced(message["content"])])
        rendered = "\n\n".join(section) + "\n"
        sections.append(rendered)
        files[f"messages/{index:04d}-{safe_name(message['remoteID'])}.md"] = rendered.replace(
            "](attachments/", "](../attachments/"
        )
    transcript = intro + "\n" + "\n".join(sections)
    if not messages:
        transcript += "\n_No retained messages._\n"
    if kind == "md":
        response = HttpResponse(transcript, content_type="text/markdown; charset=utf-8")
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("conversation.md", transcript)
            archive.writestr("manifest.json", json_text(metadata))
            archive.writestr("messages.json", json_text(messages))
            for path, content in files.items():
                archive.writestr(path, content)
        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    name = safe_name(f"{session.worker.name}-{session.remote_id}")
    response["Content-Disposition"] = f'attachment; filename="{name}.{kind}"'
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    return response
