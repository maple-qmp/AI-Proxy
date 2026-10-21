import json

from ai_proxy.prompt_compat import prepare_messages
from ai_proxy.transport import GatewayTransport, _collect_chat_stream, _collect_responses_stream


IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."


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


def test_byte_exact_against_old_gateway_fingerprint_fixture():
    source = ("x-anthropic-billing-header: private\n" + IDENTITY +
              " Refuse DoS and DDoS. Claude Code uses OpenAI; notopenai and OpenAIClient are unrelated.")
    output, categories = prepare_messages([{"role": "system", "content": source}])
    assert output[0]["content"] == ("You are an interactive software engineering assistant operating in a command-line "
        "environment. Refuse D\u200boS and D\u200bDoS. C\u200blaude Code uses O\u200bpenAI; "
        "notopenai and OpenAIClient are unrelated.")
    assert categories == ["claude_code_identity", "billing_header", "security_term", "client_brand_term"]


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
