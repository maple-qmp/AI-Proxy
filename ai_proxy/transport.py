"""Rewrite only final CodeBuddy wire payloads and record safe diagnostics."""

import json
import logging
import time
import uuid
from typing import Any

import httpx

from .prompt_compat import prepare_messages

log = logging.getLogger("ai_proxy.codebuddy")
_SEARCH_TOOLS = {"tool_search_tool_bm25", "tool_search_tool_regex",
                 "tool_search_tool_bm25_20251119", "tool_search_tool_regex_20251119"}


def _eager_deferred_tools(body: dict[str, Any]) -> bool:
    """Adapt LiteLLM's translated tool list for backends without native tool search."""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    if not any(isinstance(tool, dict) and (tool.get("type") in _SEARCH_TOOLS or
               tool.get("name") in _SEARCH_TOOLS or
               (isinstance(tool.get("function"), dict) and tool["function"].get("name") in _SEARCH_TOOLS))
               for tool in tools):
        return False
    adapted = []
    for tool in tools:
        if not isinstance(tool, dict):
            adapted.append(tool)
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name")
        if tool.get("type") in _SEARCH_TOOLS or name in _SEARCH_TOOLS:
            continue
        tool = dict(tool)
        tool.pop("defer_loading", None)
        tool.pop("custom", None)
        if isinstance(tool.get("function"), dict):
            tool["function"] = dict(tool["function"])
            tool["function"].pop("defer_loading", None)
            tool["function"].pop("custom", None)
            if isinstance(tool["function"].get("parameters"), dict):
                tool["function"]["parameters"] = dict(tool["function"]["parameters"])
                tool["function"]["parameters"].pop("defer_loading", None)
        adapted.append(tool)
    if adapted:
        body["tools"] = adapted
    else:
        body.pop("tools", None)
        body.pop("tool_choice", None)
    return True


def _collect_chat_stream(raw: bytes) -> dict[str, Any]:
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    usage: dict[str, Any] | None = None
    response_id = ""
    model = ""
    created = 0
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line[6:])
        response_id = event.get("id", response_id)
        model = event.get("model", model)
        created = event.get("created", created)
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                current = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                current["id"] += call.get("id") or ""
                current["function"]["name"] += (call.get("function") or {}).get("name") or ""
                current["function"]["arguments"] += (call.get("function") or {}).get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"id": response_id, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def _collect_responses_stream(raw: bytes) -> dict[str, Any]:
    completed: dict[str, Any] | None = None
    items: dict[int, dict[str, Any]] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line[6:])
        if event.get("type") == "response.output_item.done" and isinstance(event.get("item"), dict):
            items[event.get("output_index", len(items))] = event["item"]
        elif event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            completed = event["response"]
        elif event.get("type") in ("response.failed", "error"):
            raise ValueError("Upstream Responses stream failed")
    if completed is None:
        raise ValueError("Upstream Responses stream did not complete")
    if items:
        completed["output"] = [items[index] for index in sorted(items)]
    return completed


class GatewayTransport(httpx.AsyncBaseTransport):
    def __init__(self, upstream: httpx.AsyncBaseTransport | None = None, *, codebuddy: bool = False,
                 prompt_compat: bool = True, debug: bool = False, subscription: bool = False):
        self.upstream = upstream or httpx.AsyncHTTPTransport()
        self.codebuddy = codebuddy
        self.prompt_compat = prompt_compat
        self.debug = debug
        self.subscription = subscription

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        rewrite_categories: list[str] = []
        path = request.url.path
        generation = path.endswith("/chat/completions")
        collect_codebuddy = False
        collect_subscription = False
        if self.codebuddy and request.url.scheme == "https" and request.url.host == "copilot.tencent.com" and path == "/v2/chat/completions":
            body = json.loads(await request.aread())
            if isinstance(body, dict) and isinstance(body.get("messages"), list):
                messages, rewrite_categories = prepare_messages(body["messages"], self.prompt_compat)
                adapted_tools = _eager_deferred_tools(body)
                collect_codebuddy = body.get("stream") is not True
                if collect_codebuddy:
                    body["stream"] = True
                if rewrite_categories or collect_codebuddy or adapted_tools:
                    body["messages"] = messages
                    request = self._replace_body(request, body)
        elif self.subscription and path.endswith("/responses"):
            body = json.loads(await request.aread())
            if isinstance(body, dict):
                _eager_deferred_tools(body)
                for key in ("max_tokens", "max_output_tokens", "max_completion_tokens", "temperature", "metadata"):
                    body.pop(key, None)
                body["store"] = False
                if isinstance(body.get("input"), str):
                    body["input"] = [{"role": "user", "content": body["input"]}]
                collect_subscription = body.get("stream") is not True
                if collect_subscription:
                    body["stream"] = True
                request = self._replace_body(request, body)

        started = time.monotonic()
        request_id = uuid.uuid4().hex
        if self.debug and self.codebuddy and (generation or path.endswith("/enterprises/personal/models")):
            log.info(json.dumps({"event": "request", "id": request_id,
                "endpoint": "chat/completions" if generation else "models",
                "prompt_compat_rewrites": rewrite_categories}, separators=(",", ":")))
        try:
            response = await self.upstream.handle_async_request(request)
        except Exception:
            if self.debug and self.codebuddy:
                log.info(json.dumps({"event": "transport_error", "id": request_id,
                    "duration_ms": int((time.monotonic() - started) * 1000), "category": "fetch_failed"}))
            raise
        if collect_codebuddy and response.status_code < 400 and "text/event-stream" in response.headers.get("content-type", ""):
            try:
                collected = _collect_chat_stream(await response.aread())
                response = httpx.Response(200, headers={"content-type": "application/json"}, json=collected, request=request)
            except (ValueError, KeyError, TypeError) as exc:
                raise httpx.ProtocolError("Invalid CodeBuddy chat stream") from exc
        if collect_subscription and response.status_code < 400:
            raw = await response.aread()
            if raw.startswith(b"event:") or raw.startswith(b"data:"):
                try:
                    collected = _collect_responses_stream(raw)
                    response = httpx.Response(200, headers={"content-type": "application/json"}, json=collected, request=request)
                except (ValueError, KeyError, TypeError) as exc:
                    raise httpx.ProtocolError("Invalid ChatGPT Responses stream") from exc
        if self.debug and self.codebuddy:
            entry: dict[str, Any] = {"event": "response", "id": request_id,
                "status": response.status_code, "duration_ms": int((time.monotonic() - started) * 1000)}
            if response.status_code >= 400:
                try:
                    raw = await response.aread()
                    if len(raw) <= 32768:
                        error = json.loads(raw)
                        if isinstance(error, dict):
                            if isinstance(error.get("code"), int):
                                entry["upstream_code"] = error["code"]
                            request_id_value = error.get("requestId")
                            if isinstance(request_id_value, str) and len(request_id_value) <= 128 and request_id_value.replace("-", "").isalnum():
                                entry["upstream_request_id"] = request_id_value
                            if error.get("code") == 11128:
                                entry["category"] = "upstream_rejected_11128"
                    response = httpx.Response(response.status_code, headers=response.headers, content=raw, request=request)
                except (ValueError, httpx.HTTPError):
                    pass
            log.info(json.dumps(entry, separators=(",", ":")))
        return response

    @staticmethod
    def _replace_body(request: httpx.Request, body: dict[str, Any]) -> httpx.Request:
        headers = dict(request.headers)
        headers.pop("content-length", None)
        return httpx.Request(request.method, request.url, headers=headers,
                             content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
                             extensions=request.extensions)

    async def aclose(self) -> None:
        await self.upstream.aclose()
