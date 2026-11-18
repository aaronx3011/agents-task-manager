"""PM application budgets. Worker/provider model limits remain externally owned."""

import json

from django.conf import settings

from . import services


def limits():
    return {
        "max_messages": settings.PM_MAX_MESSAGES,
        "max_history_bytes": settings.PM_MAX_HISTORY_BYTES,
        "max_output_tokens": settings.PM_MAX_OUTPUT_TOKENS,
        "context_window_tokens": settings.PM_CONTEXT_WINDOW_TOKENS,
        "context_accounting": "conservative_utf8_bytes",
    }


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def check_history(messages, *, reserve=0):
    if len(messages) + reserve > settings.PM_MAX_MESSAGES:
        full("message count", settings.PM_MAX_MESSAGES)
    if json_bytes(messages) > settings.PM_MAX_HISTORY_BYTES:
        full("history bytes", settings.PM_MAX_HISTORY_BYTES)


def check_context(payload):
    # One token per UTF-8 byte is a deliberately conservative upper bound for
    # byte-level provider tokenizers, not an exact usage measurement. Include
    # the system prompt, tool schemas, chat framing allowance and output reserve.
    # No dependency on a different model's tokenizer or misleading chars/4 guess.
    bound = json_bytes(payload) + 256 * (len(payload["messages"]) + len(payload["tools"]))
    if bound + settings.PM_MAX_OUTPUT_TOKENS > settings.PM_CONTEXT_WINDOW_TOKENS:
        full("context token budget (conservative estimate)", settings.PM_CONTEXT_WINDOW_TOKENS)


def full(name, maximum):
    raise services.TaskError(
        f"PM conversation {name} limit ({maximum}) reached; saved history and completed "
        "tool actions are preserved. Clear the conversation to start another; "
        "check task state before repeating any action.",
        409,
    )
