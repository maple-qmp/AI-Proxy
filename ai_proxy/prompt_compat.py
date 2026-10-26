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
    "Gemini", "Kimi", "Qwen", "Cursor", "OpenCode", "agent-identity", "OhMyOpenCode", "DeepSeek",
)
_KINDS = {**{x.lower(): "security_term" for x in SECURITY_TERMS},
          **{x.lower(): "client_brand_term" for x in CLIENT_TERMS}}
_PATTERN = re.compile(r"\b(?:" + "|".join(re.escape(x) for x in sorted(SECURITY_TERMS + CLIENT_TERMS, key=len, reverse=True)) + r")\b", re.I | re.ASCII)
_HARNESS_OPENING = "You are an AI agent powered by DeepSeek Harness."
_GUI_OPENING = "You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names."
_GUI_DOUBLE_QUOTES = '"this page", "this GUI", or "this app"'
_GUI_SINGLE_QUOTES = "'this page', 'this GUI', or 'this app'"


def _remove_harness_opening(value: str) -> str:
    for newline in ("\r\n", "\n"):
        prefix = _HARNESS_OPENING + newline
        if value.startswith(prefix) and len(value) > len(prefix):
            return value[len(prefix):].removeprefix(newline)
    return value


def _rewrite_gui_quotes(value: str) -> str:
    if _GUI_DOUBLE_QUOTES not in value:
        return value
    start = 0
    while (index := value.find(_GUI_OPENING, start)) >= 0:
        if index == 0 or value[index - 1] == "\n":
            return value.replace(_GUI_DOUBLE_QUOTES, _GUI_SINGLE_QUOTES)
        start = index + len(_GUI_OPENING)
    return value


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

    without_opening = _remove_harness_opening(value)
    if without_opening != value:
        value = without_opening
        if "deepseek_harness_opening" not in matches:
            matches.append("deepseek_harness_opening")
    with_gui_quotes = _rewrite_gui_quotes(value)
    if with_gui_quotes != value:
        value = with_gui_quotes
        if "deepseek_harness_gui_quotes" not in matches:
            matches.append("deepseek_harness_gui_quotes")

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
