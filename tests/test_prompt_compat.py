import json
from copy import deepcopy

import pytest

from ai_proxy.prompt_compat import prepare_messages
from ai_proxy.transport import GatewayTransport, _collect_chat_stream, _collect_responses_stream


IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
HARNESS_OPENING = "You are an AI agent powered by DeepSeek Harness."
GUI_OPENING = "You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names."
GUI_QUOTES = '"this page", "this GUI", or "this app"'
GUI_QUOTES_REWRITTEN = "'this page', 'this GUI', or 'this app'"


def test_exact_rewrites_then_terms_only_in_system_and_idempotent():
    messages = [
        {"role": "system", "content": "x-anthropic-billing-header: private\n" + IDENTITY +
         " Main branch (you will usually use this for PRs): main. DDoS and OpenAI."},
        {"role": "system", "content": [{"type": "text", "text": "Claude Code"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]},
        {"role": "user", "content": IDENTITY}, {"role": "assistant", "content": IDENTITY},
        {"role": "tool", "content": IDENTITY},
    ]
    output, categories = prepare_messages(messages)
    assert categories == ["claude_code_identity", "main_branch_hint", "billing_header", "security_term", "client_brand_term"]
    assert output[0]["content"].startswith("You are an interactive software engineering assistant")
    assert "private" not in output[0]["content"]
    assert "D\u200bDoS" in output[0]["content"]
    assert output[1]["content"][0]["text"] == "C\u200blaude Code"
    assert output[1]["content"][1] == messages[1]["content"][1]
    assert output[2:] == messages[2:]
    assert prepare_messages(output) == (output, [])
    assert prepare_messages(messages, False) == (messages, [])


def test_transport_rewrite_after_final_litellm_body():
    import asyncio
    import httpx

    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    transport = GatewayTransport(httpx.MockTransport(upstream), codebuddy=True)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post("https://copilot.tencent.com/v2/chat/completions", json={
                "messages": [{"role": "system", "content": IDENTITY}, {"role": "user", "content": IDENTITY}]})
    asyncio.run(run())
    assert "interactive software engineering assistant" in seen[0]["messages"][0]["content"]
    assert seen[0]["messages"][1]["content"] == IDENTITY


@pytest.mark.parametrize("codebuddy,enabled,url", [
    (True, False, "https://copilot.tencent.com/v2/chat/completions"),
    (False, True, "https://api.openai.com/v1/chat/completions"),
])
def test_harness_wire_scope_and_other_roles(codebuddy, enabled, url):
    import asyncio
    import httpx

    source = HARNESS_OPENING + "\nKeep this line."
    messages = [{"role": role, "content": source} for role in ("system", "user", "assistant", "tool")]
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(transport=GatewayTransport(
                httpx.MockTransport(upstream), codebuddy=codebuddy, prompt_compat=enabled)) as client:
            await client.post(url, json={"messages": messages, "stream": True})

    asyncio.run(run())
    assert seen == [messages]
    assert messages[0]["content"] == source


def test_harness_wire_only_changes_system_role():
    import asyncio
    import httpx

    source = HARNESS_OPENING + "\nKeep this line."
    messages = [{"role": role, "content": source} for role in ("system", "user", "assistant", "tool")]
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(transport=GatewayTransport(
                httpx.MockTransport(upstream), codebuddy=True)) as client:
            await client.post("https://copilot.tencent.com/v2/chat/completions",
                              json={"messages": messages, "stream": True})

    asyncio.run(run())
    assert seen[0][0]["content"] == "Keep this line."
    assert seen[0][1:] == messages[1:]
    assert messages[0]["content"] == source


def test_byte_exact_against_old_gateway_fingerprint_fixture():
    source = ("x-anthropic-billing-header: private\n" + IDENTITY +
              " Refuse DoS and DDoS. Claude Code uses OpenAI; notopenai and OpenAIClient are unrelated.")
    output, categories = prepare_messages([{"role": "system", "content": source}])
    assert output[0]["content"] == ("You are an interactive software engineering assistant operating in a command-line "
        "environment. Refuse D\u200boS and D\u200bDoS. C\u200blaude Code uses O\u200bpenAI; "
        "notopenai and OpenAIClient are unrelated.")
    assert categories == ["claude_code_identity", "billing_header", "security_term", "client_brand_term"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("blank_line", [False, True])
def test_deepseek_harness_opening_removal(newline, blank_line):
    source = HARNESS_OPENING + newline + (newline if blank_line else "") + "Keep these instructions."
    messages = [{"role": "system", "content": source}, {"role": "user", "content": source}]
    original = deepcopy(messages)
    output, categories = prepare_messages(messages)
    assert output[0]["content"] == "Keep these instructions."
    assert output[1] == original[1]
    assert messages == original
    assert categories == ["deepseek_harness_opening"]
    assert prepare_messages(output) == (output, [])


def test_deepseek_harness_prompt_excerpt_and_term_boundaries():
    source = (HARNESS_OPENING + "\n\n" +
              "You are a coding agent powered by the deepseek-v4.1-flash model.\n\n" +
              GUI_OPENING + " When the user refers to " + GUI_QUOTES +
              " without naming another target, they mean this GUI.")
    messages = [{"role": "system", "content": source},
                {"role": "system", "content": "DeepSeek deepseek DeepSeeker"},
                {"role": "assistant", "content": source}, {"role": "tool", "content": source}]
    original = deepcopy(messages)
    output, categories = prepare_messages(messages)
    assert output[0]["content"] == (
        "You are a coding agent powered by the d\u200beepseek-v4.1-flash model.\n\n" +
        "You are interacting with the user through the D\u200beepSeek Harness Web GUI whose URL the runtime context names. " +
        "When the user refers to " + GUI_QUOTES_REWRITTEN +
        " without naming another target, they mean this GUI.")
    assert output[1]["content"] == "D\u200beepSeek d\u200beepseek DeepSeeker"
    assert output[2:] == original[2:]
    assert messages == original
    assert categories == ["deepseek_harness_opening", "deepseek_harness_gui_quotes", "client_brand_term"]
    assert prepare_messages(output) == (output, [])
    assert prepare_messages(messages, False) == (messages, [])


@pytest.mark.parametrize("source,want", [
    (HARNESS_OPENING, "You are an AI agent powered by D\u200beepSeek Harness."),
    ("Preamble\n" + HARNESS_OPENING + "\nKeep this line.",
     "Preamble\nYou are an AI agent powered by D\u200beepSeek Harness.\nKeep this line."),
    (GUI_QUOTES, GUI_QUOTES),
    ("Prefix " + GUI_OPENING + "\n" + GUI_QUOTES,
     "Prefix You are interacting with the user through the D\u200beepSeek Harness Web GUI whose URL the runtime context names.\n" + GUI_QUOTES),
    (GUI_OPENING + "\r\n" + GUI_QUOTES,
     "You are interacting with the user through the D\u200beepSeek Harness Web GUI whose URL the runtime context names.\r\n" + GUI_QUOTES_REWRITTEN),
])
def test_deepseek_harness_nonmatching_cases(source, want):
    output, _ = prepare_messages([{"role": "system", "content": source}])
    assert output[0]["content"] == want
    assert prepare_messages(output) == (output, [])


def test_deepseek_harness_text_block_and_other_blocks_unchanged():
    source = HARNESS_OPENING + "\n\n" + GUI_OPENING + " " + GUI_QUOTES
    messages = [{"role": "system", "content": [
        {"type": "text", "text": source}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]},
        {"role": "user", "content": [{"type": "text", "text": source}]}]
    original = deepcopy(messages)
    output, _ = prepare_messages(messages)
    assert output[0]["content"][0]["text"] == (
        "You are interacting with the user through the D\u200beepSeek Harness Web GUI whose URL the runtime context names. " + GUI_QUOTES_REWRITTEN)
    assert output[0]["content"][1] == original[0]["content"][1]
    assert output[1] == original[1]
    assert messages == original


def test_stream_only_upstream_collection_preserves_tool_calls_and_output_items():
    chat_events = [
        {"id": "chat_1", "created": 1, "model": "glm", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "Read", "arguments": '{"file_path":'}}]}, "finish_reason": None}]},
        {"id": "chat_1", "created": 1, "model": "glm", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"a.txt"}'}}]}, "finish_reason": "tool_calls"}]},
    ]
    chat = _collect_chat_stream("".join("data: " + json.dumps(x) + "\n\n" for x in chat_events).encode())
    assert chat["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"file_path":"a.txt"}'
    output = {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}
    response_events = [
        {"type": "response.output_item.done", "output_index": 0, "item": output},
        {"type": "response.completed", "response": {"id": "resp_1", "object": "response", "output": [], "status": "completed"}},
    ]
    response = _collect_responses_stream("".join("data: " + json.dumps(x) + "\n\n" for x in response_events).encode())
    assert response["output"] == [output]
