import asyncio
import json

import httpx
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

import ai_proxy.app as gateway
from ai_proxy.auth import Session
from ai_proxy.catalog import Model
from ai_proxy.transport import GatewayTransport


def _completion(message=None):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "glm",
            "choices": [{"index": 0, "message": message or {"role": "assistant", "content": "hi"}, "finish_reason": "tool_calls" if message and message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}}


def _app(monkeypatch, owner="codebuddy", upstream=None):
    app = gateway.create_app()
    session = Session("identity", "secret", {}, "https://copilot.tencent.com/v2" if owner == "codebuddy" else "https://api.openai.com/v1", owner == "openai")
    model = Model("glm", owner, images=True)

    async def resolve(_name):
        return session, model

    async def listed():
        return [model]

    app.state.catalog.resolve = resolve
    app.state.catalog.list = listed
    monkeypatch.setattr(gateway, "_handler", lambda _model, _session: AsyncHTTPHandler(
        transport=GatewayTransport(httpx.MockTransport(upstream or (lambda _req: httpx.Response(200, json=_completion()))),
                                   codebuddy=owner == "codebuddy", subscription=owner == "openai")))
    return app


def test_three_endpoints_and_codebuddy_system_rewrite(monkeypatch):
    seen = []

    def upstream(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=_completion())

    app = _app(monkeypatch, upstream=upstream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/healthz")).json() == {"ok": True}
            assert (await client.get("/v1/models")).json()["data"][0]["owned_by"] == "codebuddy"
            chat = await client.post("/v1/chat/completions", json={"model": "glm", "messages": [
                {"role": "system", "content": "You are Claude Code, Anthropic's official CLI for Claude."},
                {"role": "user", "content": "You are Claude Code, Anthropic's official CLI for Claude."}]})
            assert chat.status_code == 200
            assert chat.json()["choices"][0]["message"]["content"] == "hi"
            responses = await client.post("/v1/responses", json={"model": "glm", "input": "Hi"})
            assert responses.status_code == 200
            assert responses.json()["output"][0]["content"][0]["text"] == "hi"
            messages = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 100,
                "system": "You are Claude Code, Anthropic's official CLI for Claude.",
                "messages": [{"role": "user", "content": "Hi"}]})
            assert messages.status_code == 200
            assert messages.json()["content"][0]["text"] == "hi"
    asyncio.run(run())
    assert all(path == "/v2/chat/completions" for path, _ in seen)
    assert "interactive software engineering assistant" in seen[0][1]["messages"][0]["content"]
    assert seen[0][1]["messages"][1]["content"] == "You are Claude Code, Anthropic's official CLI for Claude."
    assert "interactive software engineering assistant" in seen[2][1]["messages"][0]["content"]


def test_messages_tool_roundtrip_and_streaming(monkeypatch):
    seen = []

    def upstream(request):
        body = json.loads(request.content)
        seen.append(body)
        if body.get("stream"):
            wants_tool = body.get("tools") and not any(x.get("role") == "tool" for x in body["messages"])
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "lookup", "arguments": '{"key":"a"}'}}]} if wants_tool else {"role": "assistant", "content": "hi"}
            chunks = [{"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "glm", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "glm", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if wants_tool else "stop"}]}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n")
        if any(x.get("role") == "tool" for x in body["messages"]):
            return httpx.Response(200, json=_completion())
        return httpx.Response(200, json=_completion({"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"key":"a"}'}}]}))

    app = _app(monkeypatch, upstream=upstream)
    tool = {"name": "lookup", "description": "Lookup", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}}}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 100,
                "messages": [{"role": "user", "content": "Look up a"}], "tools": [tool]})
            assert first.status_code == 200, first.text
            use = next(x for x in first.json()["content"] if x["type"] == "tool_use")
            second = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 100,
                "messages": [{"role": "user", "content": "Look up a"},
                             {"role": "assistant", "content": [use]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "found"}]}],
                "tools": [tool]})
            assert second.status_code == 200, second.text
            assert second.json()["content"][0]["text"] == "hi"
            stream = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}], "stream": True})
            assert stream.status_code == 200
            assert "event: message_start" in stream.text
            assert "event: message_stop" in stream.text
            assert "[DONE]" not in stream.text
    asyncio.run(run())
    assert any(x.get("role") == "tool" and x.get("content") == "found" for x in seen[1]["messages"])


def test_known_unsupported_codebuddy_named_tool_returns_anthropic_error(monkeypatch):
    app = _app(monkeypatch)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for field in ({"tool_choice": {"type": "tool", "name": "Read"}},):
                result = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 100,
                    "messages": [{"role": "user", "content": "Hi"}], **field})
                assert result.status_code == 400
                assert result.json()["type"] == "error"
    asyncio.run(run())


def test_auto_mode_safeguards_uses_classifier_and_stream_delta(monkeypatch):
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) % 2 == 0:
            return httpx.Response(200, json=_completion({"role": "assistant", "content":
                '{"results":[{"id":"call_1","outcome":"not_flagged"}]}'}))
        return httpx.Response(200, json=_completion({"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"a.txt"}'}}]}))

    app = _app(monkeypatch, upstream=upstream)
    payload = {"model": "glm", "max_tokens": 100, "messages": [{"role": "user", "content": "Read a.txt"}],
               "tools": [{"name": "Read", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}],
               "safeguards": [{"type": "dangerous_tool_use", "classifier_context": {"cwd": "/tmp"}}]}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/messages", json=payload)
            assert response.status_code == 200, response.text
            verdict = response.json()["safeguard_results"][0]["status"]["tool_uses"]["call_1"]
            assert verdict == {"type": "evaluated", "outcome": "not_flagged"}
            stream = await client.post("/v1/messages", json={**payload, "stream": True})
            assert stream.status_code == 200, stream.text
            assert '"safeguard_results"' in stream.text
            assert "event: message_stop" in stream.text
    asyncio.run(run())
    assert len(requests) == 4


def test_chatgpt_chat_uses_subscription_responses_path(monkeypatch):
    paths = []

    def upstream(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "created_at": 1, "model": "glm", "status": "completed",
            "output": [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "hi", "annotations": []}]}],
            "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
        })

    app = _app(monkeypatch, owner="openai", upstream=upstream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={
                "model": "glm", "messages": [{"role": "user", "content": "Hi"}]})
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == "hi"

    asyncio.run(run())
    assert paths == ["/v1/responses"]


def test_messages_image_reaches_litellm_chat_conversion(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion())

    app = _app(monkeypatch, upstream=upstream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 64,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
                ]}]})
            assert response.status_code == 200, response.text

    asyncio.run(run())
    content = seen[0]["messages"][0]["content"]
    assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
               for part in content)


def test_disconnect_cancels_nonstream_generation_task():
    class Disconnected:
        async def is_disconnected(self):
            return True

    async def run():
        pending = asyncio.create_task(asyncio.sleep(60))
        await gateway._cancel_on_disconnect(Disconnected(), pending)
        try:
            await pending
        except asyncio.CancelledError:
            pass
        assert pending.cancelled()

    asyncio.run(run())


def test_litellm_thinking_conversion_survives_codebuddy_stream_collection(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        chunks = [
            {"id": "chat_1", "object": "chat.completion.chunk", "created": 1, "model": "glm", "choices": [
                {"index": 0, "delta": {"role": "assistant", "reasoning_content": "I checked."}, "finish_reason": None}]},
            {"id": "chat_1", "object": "chat.completion.chunk", "created": 1, "model": "glm", "choices": [
                {"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n")

    app = _app(monkeypatch, upstream=upstream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 128,
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": [{"role": "user", "content": "Reply OK"}]})
            assert response.status_code == 200, response.text
            assert [block["type"] for block in response.json()["content"]] == ["thinking", "text"]
            assert response.json()["content"][0]["thinking"] == "I checked."

    asyncio.run(run())
    assert seen[0]["reasoning_effort"] in ("low", "medium", "high")


def test_tool_search_deferred_tools_are_available_without_native_search(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_completion({"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": '{"file_path":"a.txt"}'}}]}))

    app = _app(monkeypatch, upstream=upstream)
    tools = [{"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"},
             {"name": "Read", "description": "Read file", "input_schema": {"type": "object", "properties": {
                 "file_path": {"type": "string"}}}, "custom": {"defer_loading": True}}]

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 128,
                "messages": [{"role": "user", "content": "Read a.txt"}], "tools": tools})
            assert response.status_code == 200, response.text
            assert any(block["type"] == "tool_use" and block["name"] == "Read" for block in response.json()["content"])

    asyncio.run(run())
    assert [tool["function"]["name"] for tool in seen[0]["tools"]] == ["Read"]
    assert "defer_loading" not in seen[0]["tools"][0]["function"]["parameters"]


def test_subscription_responses_tool_search_uses_litellm_functions(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp_1", "object": "response", "created_at": 1,
            "model": "glm", "status": "completed", "output": [{"id": "msg_1", "type": "message",
            "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "OK",
            "annotations": []}]}], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}})

    app = _app(monkeypatch, owner="openai", upstream=upstream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/messages", json={"model": "glm", "max_tokens": 128,
                "messages": [{"role": "user", "content": "Read a.txt"}], "tools": [
                    {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"},
                    {"name": "Read", "description": "Read file", "input_schema": {"type": "object", "properties": {}}}]})
            assert response.status_code == 200, response.text

    asyncio.run(run())
    assert [tool["name"] for tool in seen[0]["tools"]] == ["Read"]
