"""CodeBuddy system-prompt compatibility rules."""

import re
from copy import deepcopy
from typing import Any

FINGERPRINTS = (
    ("You are Claude Code, Anthropic's official CLI for Claude.", "You are an interactive software engineering assistant operating in a command-line environment.", "claude_code_identity"),
    ("You are Claude Code, Anthropic's official CLI", "You are an interactive software engineering assistant operating in a command-line environment", "claude_code_identity"),
    ("Main branch (you will usually use this for PRs)", "Main branch", "main_branch_hint"),
)
SECURITY_TERMS = (
    "DoS", "DDoS", "exploit", "credential testing", "credential stuffing",
    "supply chain compromise", "supply-chain compromise", "detection evasion",
    "C2 frameworks", "C2 framework", "command and control", "malicious purposes",
    "malicious intent", "mass targeting", "brute force", "brute-force",
    "privilege escalation", "reverse shell", "remote code execution", "SQL injection",
    "XSS", "CSRF", "phishing", "malware", "ransomware", "keylogger",
    "rootkit", "backdoor", "botnet", "zero-day", "0day",
)
CLIENT_TERMS = (
    "Claude Code", "Claude Opus", "Claude Sonnet", "Claude Haiku", "Claude Fable",
    "Anthropic", "Co-Authored-By", "noreply@anthropic.com", "Claude", "OpenAI",
    "Gemini", "Kimi", "Qwen", "Cursor", "OpenCode", "agent-identity", "OhMyOpenCode",
)
_KINDS = {**{x.lower(): "security_term" for x in SECURITY_TERMS},
          **{x.lower(): "client_brand_term" for x in CLIENT_TERMS}}
_PATTERN = re.compile(r"\b(?:" + "|".join(re.escape(x) for x in sorted(SECURITY_TERMS + CLIENT_TERMS, key=len, reverse=True)) + r")\b", re.I | re.ASCII)


def _rewrite(value: str, matches: list[str]) -> str:
    for before, after, category in FINGERPRINTS:
        if before in value:
            value = value.replace(before, after)
            if category not in matches:
                matches.append(category)
    if value.startswith("x-anthropic-billing-header:"):
        _, sep, tail = value.partition("\n")
        value = tail if sep else ""
        if "billing_header" not in matches:
            matches.append("billing_header")

    def replace(match: re.Match[str]) -> str:
        if match.start() > 0 and match.string[match.start() - 1] == "\u200b":
            return match.group()
        category = _KINDS.get(match.group().lower(), "term")
        if category not in matches:
            matches.append(category)
        word = match.group()
        return word[0] + "\u200b" + word[1:]

    # The longer rewritten word can expose a shorter term after its zero-width
    # character; the preceding-character check keeps the pass idempotent.
    return _PATTERN.sub(replace, value)


def prepare_messages(messages: list[dict[str, Any]], enabled: bool = True) -> tuple[list[dict[str, Any]], list[str]]:
    if not enabled:
        return messages, []
    output = deepcopy(messages)
    matches: list[str] = []
    for message in output:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = _rewrite(content, matches)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    block["text"] = _rewrite(block["text"], matches)
    return (output if matches else messages), matches
