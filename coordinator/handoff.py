"""Hand-off mode policy (#431): prompt classification and auto-answer deny rules.

Pure functions only; the sync-side wiring lives in services.maybe_auto_answer.
Anything that cannot be confidently classified as a binary approve/deny prompt
requires a human, and any deny-rule match is escalated rather than approved.
"""

import json
import re

from django.conf import settings

APPROVE_DENY = "approve_deny"
MULTIPLE_CHOICE = "multiple_choice"
FREE_TEXT = "free_text"
UNKNOWN = "unknown"

# (rule name, case-insensitive regex) searched across the whole prompt record.
DEFAULT_DENY_RULES = (
    ("production", r"\b(prod|production|live (service|server|site|system)s?)\b"),
    ("deploy", r"\bdeploy\w*"),
    ("dns", r"\bdns\b"),
    ("billing", r"\b(billing|payments?|invoices?|paid api|subscriptions?)\b"),
    (
        "credentials",
        r"(credentials?|secrets?|api[_ -]?keys?|\btokens?\b|passwords?|private[_ -]?keys?"
        r"|\.env\b|ssh[_ -]?keys?)",
    ),
    (
        "destructive_git",
        r"\bgit\s+(reset\s+--hard|clean\b|rebase\b|filter-(branch|repo)|commit\s+--amend"
        r"|branch\s+(-d\b|-D\b|--delete)|push\b[^\n]*(--force|--delete|\s-f\b|\s:\S))"
        r"|force[- ]push|history rewrite|delete (the )?branch",
    ),
    (
        "destructive_delete",
        r"\b(rm|rmdir|unlink|shred|truncate|drop\s+(table|database|schema)|delete|overwrite"
        r"|wipe|purge|prune|destroy)\b"
        r"|\bdocker\s+(rm|rmi|volume|system\s+prune|compose\s+down)\b",
    ),
)


def deny_rules():
    """The active (name, compiled regex) rules; settings may replace the defaults."""
    configured = getattr(settings, "HANDOFF_DENY_RULES", None)
    rules = DEFAULT_DENY_RULES if configured is None else configured
    return [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in rules]


def classify_prompt(kind, data):
    """approve_deny | multiple_choice | free_text | unknown for a pending interaction.

    Permission requests are the binary allow/deny prompts. Question batches are
    never auto-answerable: a batch where every question offers options is
    multiple_choice and one with any option-less question is free_text. A
    malformed record is unknown and always goes to a human.
    """
    if not isinstance(data, dict) or not data:
        return UNKNOWN
    if kind == "permission":
        return APPROVE_DENY
    if kind != "question":
        return UNKNOWN
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        return UNKNOWN
    if not all(isinstance(question, dict) for question in questions):
        return UNKNOWN
    if any(not question.get("options") for question in questions):
        return FREE_TEXT
    return MULTIPLE_CHOICE


def matched_deny_rule(data):
    """Name of the first deny rule matching the prompt record, or None."""
    text = json.dumps(data, default=str, ensure_ascii=False)
    for name, pattern in deny_rules():
        if pattern.search(text):
            return name
    return None


def decide(kind, data):
    """(prompt_type, action, rule): action is 'approve' or 'escalate'."""
    prompt_type = classify_prompt(kind, data)
    if prompt_type != APPROVE_DENY:
        return prompt_type, "escalate", None
    rule = matched_deny_rule(data)
    if rule:
        return prompt_type, "escalate", rule
    return prompt_type, "approve", None
