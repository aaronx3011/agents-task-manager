"""Minimal Notion REST client for the outbound mirror (GH #94).

Only the calls the sync needs: read a data source schema, query its pages, create
and update pages, and replace a page body. Requests are paced under Notion's
average limit (~3 requests/second) and honour ``429 Retry-After``. The token is
sent only in the Authorization header and never logged.
"""

import time

import httpx

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
RICH_TEXT_CHUNK = 2000
MAX_RICH_TEXT_ITEMS = 100
MAX_CHILDREN = 100


class NotionError(Exception):
    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class NotionClient:
    def __init__(self, token, *, http=None, min_interval=0.34, max_retries=4, sleep=time.sleep):
        if not token:
            raise NotionError("NOTION_TOKEN is not configured")
        self._http = http or httpx.Client(base_url=API_URL, timeout=30)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sleep = sleep
        self._last_request = 0.0

    def request(self, method, path, json=None, params=None):
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self._http.request(
                    method, path, json=json, params=params, headers=self._headers
                )
            except httpx.HTTPError as exc:
                if attempt == self._max_retries:
                    raise NotionError(f"Notion unreachable: {exc}", retryable=True) from exc
                self._sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self._max_retries:
                    raise NotionError(
                        f"Notion {response.status_code} after retries",
                        response.status_code,
                        retryable=True,
                    )
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                self._sleep(min(delay, 60))
                continue
            if response.status_code >= 400:
                try:
                    detail = response.json().get("message", "")
                except ValueError:
                    detail = response.text[:300]
                raise NotionError(f"Notion {response.status_code}: {detail}", response.status_code)
            return response.json() if response.content else {}
        raise NotionError("unreachable")  # pragma: no cover

    # --- data sources ------------------------------------------------------

    def data_source(self, data_source_id):
        return self.request("GET", f"/data_sources/{data_source_id}")

    def query_all(self, data_source_id):
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"/data_sources/{data_source_id}/query", json=body)
            yield from data.get("results", [])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    # --- pages -------------------------------------------------------------

    def create_page(self, data_source_id, properties, children=()):
        children = list(children)
        page = self.request(
            "POST",
            "/pages",
            json={
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": properties,
                "children": children[:MAX_CHILDREN],
            },
        )
        self._append(page["id"], children[MAX_CHILDREN:])
        return page

    def update_page(self, page_id, properties):
        return self.request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def replace_body(self, page_id, children):
        cursor = None
        existing = []
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.request("GET", f"/blocks/{page_id}/children", params=params)
            existing.extend(block["id"] for block in data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        for block_id in existing:
            self.request("DELETE", f"/blocks/{block_id}")
        self._append(page_id, list(children))

    def _append(self, page_id, children):
        for start in range(0, len(children), MAX_CHILDREN):
            self.request(
                "PATCH",
                f"/blocks/{page_id}/children",
                json={"children": children[start : start + MAX_CHILDREN]},
            )


# --- value encoding ---------------------------------------------------------


def rich_text(text):
    text = text or ""
    chunks = [text[i : i + RICH_TEXT_CHUNK] for i in range(0, len(text), RICH_TEXT_CHUNK)]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks][:MAX_RICH_TEXT_ITEMS]


def encode(prop_type, value):
    """Encode a Python value for a Notion property of the given schema type."""
    if prop_type == "title":
        return {"title": rich_text(value)}
    if prop_type == "rich_text":
        return {"rich_text": rich_text(value)}
    if prop_type == "number":
        return {"number": value}
    if prop_type == "url":
        return {"url": value or None}
    if prop_type == "select":
        return {"select": {"name": value} if value else None}
    if prop_type == "status":
        return {"status": {"name": value} if value else None}
    if prop_type == "date":
        return {"date": {"start": value} if value else None}
    if prop_type == "relation":
        return {"relation": [{"id": page_id} for page_id in value or []]}
    raise NotionError(f"Unsupported Notion property type {prop_type!r}")


def plain_text(prop):
    """Read a title/rich_text/number property value back from a page."""
    if prop is None:
        return None
    kind = prop.get("type")
    if kind in {"title", "rich_text"}:
        return "".join(part.get("plain_text", "") for part in prop.get(kind) or [])
    if kind == "number":
        return prop.get("number")
    return None


def heading(text):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text(text)}}


def paragraphs(text):
    blocks = []
    for part in (text or "").replace("\r\n", "\n").split("\n\n"):
        if part.strip():
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich_text(part)},
                }
            )
    return blocks
