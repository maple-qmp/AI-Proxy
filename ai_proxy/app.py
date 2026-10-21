"""Local HTTP surface backed by LiteLLM's protocol converters."""

import asyncio
import hmac
import json
import logging
import os
import uuid
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import litellm
from openai import AsyncOpenAI
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.completion_extras.litellm_responses_transformation.transformation import LiteLLMResponsesTransformationHandler

from .auth import ChatGPTAuth, CodeBuddyAuth, Session, UpstreamUnavailable, default_auth_file
from .catalog import Model, ModelCatalog
from .transport import GatewayTransport

log = logging.getLogger("ai_proxy")
litellm.suppress_debug_info = True
litellm.drop_params = False


def _gateway_error(status: int, message: str, anthropic: bool = False, *, param: str | None = None) -> JSONResponse:
    if anthropic:
        kind = "authentication_error" if status == 401 else "not_found_error" if status == 404 else "invalid_request_error" if status < 500 else "api_error"
        return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)
    return JSONResponse({"error": {"message": message, "type": "authentication_error" if status == 401 else "invalid_request_error" if status < 500 else "server_error", "param": param, "code": None}}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    if len(request.headers.get("content-length", "")) > 20:
        raise ValueError("Request body exceeds 10 MiB")
    raw = await request.body()
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("Request body exceeds 10 MiB")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


async def _cancel_on_disconnect(request: Request, task: asyncio.Task[Any]) -> None:
    while not task.done():
        if await request.is_disconnected():
            task.cancel()
            return
        await asyncio.sleep(0.1)


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return value
    return json.loads(json.dumps(value, default=str))


def _frame(value: Any, name: str | None = None) -> bytes:
    data = json.dumps(_json(value), ensure_ascii=False, separators=(",", ":"))
    return ((f"event: {name}\n" if name else "") + f"data: {data}\n\n").encode()


def _handler(model: Model, session: Session) -> AsyncHTTPHandler:
    transport = GatewayTransport(codebuddy=model.owner == "codebuddy",
        prompt_compat=os.getenv("CODEBUDDY_PROMPT_COMPAT") != "0",
        debug=os.getenv("CODEBUDDY_DEBUG") == "1", subscription=session.subscription)
    return AsyncHTTPHandler(transport=transport)


def _kwargs(model: Model, session: Session, handler: AsyncHTTPHandler, route: str) -> dict[str, Any]:
    use_chat_client = route == "chat/completions" or model.owner == "codebuddy"
    call_client = (AsyncOpenAI(api_key=session.token, base_url=session.base_url,
                              http_client=handler.client) if use_chat_client else handler)
    return {"model": f"{'custom_openai' if model.owner == 'codebuddy' else 'openai'}/{model.id}", "api_base": session.base_url,
            "api_key": session.token, "extra_headers": session.headers, "client": call_client,
            "num_retries": 0,
            **({"allowed_openai_params": ["reasoning_effort"]} if model.owner == "codebuddy" else {})}


def _validate_features(payload: dict[str, Any], model: Model, route: str) -> None:
    if route == "messages":
        if model.owner == "codebuddy" and isinstance(payload.get("tool_choice"), dict) and payload["tool_choice"].get("type") == "tool":
            raise ValueError("CodeBuddy does not support forcing a named tool; use auto or any")
    if not model.tools and payload.get("tools"):
        raise ValueError("The selected model does not support tool calls")
    if not model.images and any(isinstance(m, dict) and any(isinstance(part, dict) and part.get("type") in ("image", "image_url", "input_image")
        for part in (m.get("content") if isinstance(m.get("content"), list) else [])) for m in payload.get("messages", []) if isinstance(payload.get("messages"), list)):
        raise ValueError("The selected model does not support image input")


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": block["id"], "name": block["name"], "input": block.get("input", {})}
            for block in message.get("content", []) if isinstance(block, dict) and block.get("type") == "tool_use"]


async def _subscription_chat(payload: dict[str, Any], model: Model, session: Session,
                             handler: AsyncHTTPHandler) -> dict[str, Any]:
    """Use LiteLLM's Chat input converter with the subscription's native Responses route."""
    bridge = LiteLLMResponsesTransformationHandler()
    input_items, instructions = bridge.convert_chat_completion_messages_to_responses_api(payload["messages"])
    request: dict[str, Any] = {"input": input_items, "store": False}
    if instructions:
        request["instructions"] = instructions
    if isinstance(payload.get("tools"), list):
        request["tools"] = [{"type": "function", "name": tool["function"]["name"],
                             "description": tool["function"].get("description", ""),
                             "parameters": tool["function"].get("parameters", {})}
                            for tool in payload["tools"] if tool.get("type") == "function"]
    if payload.get("tool_choice") in ("auto", "none", "required"):
        request["tool_choice"] = payload["tool_choice"]
    raw = await litellm.aresponses(**_kwargs(model, session, handler, "responses"), **request)
    value = _json(raw)
    contents: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in value.get("output", []):
        if item.get("type") == "message":
            contents.extend(part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text")
        elif item.get("type") == "function_call":
            calls.append({"id": item.get("call_id", item.get("id", "")), "type": "function",
                          "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")}})
    usage = value.get("usage") or {}
    message: dict[str, Any] = {"role": "assistant", "content": "".join(contents) or None}
    if calls:
        message["tool_calls"] = calls
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
            "model": model.id, "choices": [{"index": 0, "message": message,
            "finish_reason": "tool_calls" if calls else "length" if value.get("status") == "incomplete" else "stop"}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
                      "total_tokens": usage.get("total_tokens", 0)}}


async def _safeguards(payload: dict[str, Any], message: dict[str, Any], model: Model, session: Session,
                      catalog: ModelCatalog) -> list[dict[str, Any]]:
    requested = payload.get("safeguards")
    if not isinstance(requested, list):
        return []
    calls = _tool_calls(message)
    if not calls:
        return [{"type": x.get("type"), "status": {"type": "available", "tool_uses": {}}} for x in requested]
    configured = os.getenv("SAFEGUARDS_CLASSIFIER_MODEL")
    if configured:
        try:
            session, model = await catalog.resolve(configured)
        except (LookupError, UpstreamUnavailable):
            return [{"type": x.get("type"), "status": {"type": "unsupported"}} for x in requested]
    transcript = json.dumps(payload.get("messages", [])[-12:], ensure_ascii=False)[:24000]
    prompt = ("Review proposed tool calls against the user's request. Flag exfiltration, irreversible deletion, "
              "untrusted code execution, and actions beyond the request. Return only JSON as "
              "{\"results\":[{\"id\":\"tool id\",\"outcome\":\"flagged or not_flagged\",\"explanation\":\"optional\"}]}"
              f"\nEnvironment: {json.dumps(requested[0].get('classifier_context', {}), ensure_ascii=False)[:8000]}"
              f"\nTranscript: {transcript}\nPending calls: {json.dumps(calls, ensure_ascii=False)}")
    handler = _handler(model, session)
    try:
        response = await asyncio.wait_for(litellm.acompletion(**_kwargs(model, session, handler, "chat/completions"),
            messages=[{"role": "user", "content": prompt}], max_tokens=2048), 30)
        content = _json(response)["choices"][0]["message"]["content"]
        clean = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(clean)
        verdicts = {x["id"]: x for x in parsed["results"] if isinstance(x, dict) and x.get("outcome") in ("flagged", "not_flagged")}
        statuses = {call["id"]: ({"type": "evaluated", "outcome": verdicts[call["id"]]["outcome"],
            **({"explanation": str(verdicts[call["id"]].get("explanation", ""))[:500]} if verdicts[call["id"]]["outcome"] == "flagged" else {})}
            if call["id"] in verdicts else {"type": "unavailable", "reason": "error"}) for call in calls}
    except (Exception, asyncio.CancelledError):
        statuses = {call["id"]: {"type": "unavailable", "reason": "error"} for call in calls}
    finally:
        await handler.close()
    return [{"type": x.get("type"), "status": {"type": "available", "tool_uses": statuses}} for x in requested]


def _anthropic_events(message: dict[str, Any]) -> AsyncIterator[bytes]:
    async def events() -> AsyncIterator[bytes]:
        shell = {key: value for key, value in message.items() if key not in ("content", "stop_reason", "stop_sequence", "safeguard_results")}
        shell["content"] = []
        shell["stop_reason"] = None
        shell["stop_sequence"] = None
        yield _frame({"type": "message_start", "message": shell}, "message_start")
        for index, block in enumerate(message.get("content", [])):
            start = {"type": "text", "text": ""} if block.get("type") == "text" else {**block, "input": {}} if block.get("type") == "tool_use" else block
            yield _frame({"type": "content_block_start", "index": index, "content_block": start}, "content_block_start")
            delta = {"type": "text_delta", "text": block.get("text", "")} if block.get("type") == "text" else {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False)}
            yield _frame({"type": "content_block_delta", "index": index, "delta": delta}, "content_block_delta")
            yield _frame({"type": "content_block_stop", "index": index}, "content_block_stop")
        yield _frame({"type": "message_delta", "delta": {"stop_reason": message.get("stop_reason"),
            "stop_sequence": message.get("stop_sequence"), "safeguard_results": message.get("safeguard_results", [])},
            "usage": message.get("usage", {})}, "message_delta")
        yield _frame({"type": "message_stop"}, "message_stop")
    return events()


def create_app(*, client: httpx.AsyncClient | None = None, chatgpt_auth_file: Path | None = None,
               codebuddy_auth_file: Path | None = None) -> FastAPI:
    owned_client = client is None
    upstream_client = client or httpx.AsyncClient()
    auth_file = chatgpt_auth_file or (Path(os.environ["GATEWAY_AUTH_FILE"]) if os.getenv("GATEWAY_AUTH_FILE") else default_auth_file())
    cb_file = codebuddy_auth_file or (Path(os.environ["CODEBUDDY_AUTH_FILE"]) if os.getenv("CODEBUDDY_AUTH_FILE") else None)
    catalog = ModelCatalog(ChatGPTAuth(auth_file, upstream_client), CodeBuddyAuth(upstream_client, cb_file), upstream_client)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owned_client:
            await upstream_client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.catalog = catalog

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        key = os.getenv("GATEWAY_API_KEY")
        if key:
            bearer = request.headers.get("authorization", "")
            supplied = bearer[7:] if bearer.lower().startswith("bearer ") else request.headers.get("x-api-key", "") if request.url.path.startswith("/v1/messages") else ""
            if not hmac.compare_digest(supplied, key):
                return _gateway_error(401, "Incorrect API key provided", request.url.path.startswith("/v1/messages"))
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/v1/models")
    async def models():
        try:
            found = await catalog.list()
        except UpstreamUnavailable as exc:
            return _gateway_error(503, str(exc))
        return {"object": "list", "data": [{"id": x.id, "object": "model", "created": 0, "owned_by": x.owner} for x in found]}

    @app.post("/v1/{route:path}")
    async def generate(request: Request, route: str):
        if route not in ("chat/completions", "responses", "messages"):
            return _gateway_error(404, "Not found", route.startswith("messages"))
        anthropic = route == "messages"
        handler: AsyncHTTPHandler | None = None
        streaming_handoff = False
        disconnect_watcher: asyncio.Task[None] | None = None
        try:
            payload = await _body(request)
            disconnect_watcher = asyncio.create_task(_cancel_on_disconnect(request, asyncio.current_task()))
            name = payload.get("model")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("model is required")
            session, model = await catalog.resolve(name)
            _validate_features(payload, model, route)
            stream = payload.get("stream") is True
            handler = _handler(model, session)
            if route == "messages":
                kwargs = {key: value for key, value in payload.items() if key not in ("safeguards", "model", "stream")}
                kwargs.update(_kwargs(model, session, handler, route))
                # Auto mode needs a complete tool-call set before classification.
                has_safeguards = isinstance(payload.get("safeguards"), list)
                result = await litellm.anthropic_messages(**kwargs, stream=stream and not has_safeguards)
                if has_safeguards:
                    value = _json(result)
                    value["model"] = name
                    value["safeguard_results"] = await _safeguards(payload, value, model, session, catalog)
                    await handler.close()
                    if stream:
                        return StreamingResponse(_anthropic_events(value), media_type="text/event-stream")
                    return JSONResponse(value)
                if stream:
                    async def anthropic_stream() -> AsyncIterator[bytes]:
                        try:
                            async for event in result:
                                yield event if isinstance(event, bytes) else event.encode() if isinstance(event, str) else _frame(event)
                        finally:
                            await handler.close()
                    streaming_handoff = True
                    return StreamingResponse(anthropic_stream(), media_type="text/event-stream")
                await handler.close()
                value = _json(result)
                value["model"] = name
                return JSONResponse(value)
            if route == "chat/completions":
                kwargs = {key: value for key, value in payload.items() if key != "model"}
                if session.subscription:
                    result = await _subscription_chat(payload, model, session, handler)
                else:
                    result = await litellm.acompletion(**_kwargs(model, session, handler, route), **kwargs)
            else:
                kwargs = {key: value for key, value in payload.items() if key != "model"}
                if session.subscription:
                    kwargs.pop("max_output_tokens", None)
                    kwargs.pop("metadata", None)
                result = await litellm.aresponses(**_kwargs(model, session, handler, route),
                    **({"use_chat_completions_api": True} if model.owner == "codebuddy" else {}), **kwargs)
            if stream:
                async def openai_stream() -> AsyncIterator[bytes]:
                    try:
                        if session.subscription and route == "chat/completions":
                            value = _json(result)
                            choice = value["choices"][0]
                            chunk_base = {key: value[key] for key in ("id", "created", "model")}
                            chunk_base["model"] = name
                            chunk_base["object"] = "chat.completion.chunk"
                            yield _frame({**chunk_base, "choices": [{"index": 0, "delta": choice["message"], "finish_reason": None}]})
                            yield _frame({**chunk_base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]})
                        else:
                            async for event in result:
                                value = _json(event)
                                if isinstance(value, dict) and "model" in value:
                                    value["model"] = name
                                event_type = value.get("type") if route == "responses" and isinstance(value, dict) else None
                                event_name = getattr(event_type, "value", event_type)
                                yield _frame(value, event_name)
                        if route == "chat/completions":
                            yield b"data: [DONE]\n\n"
                    finally:
                        await handler.close()
                streaming_handoff = True
                return StreamingResponse(openai_stream(), media_type="text/event-stream")
            await handler.close()
            value = _json(result)
            value["model"] = name
            return JSONResponse(value)
        except LookupError as exc:
            return _gateway_error(404, str(exc), anthropic, param="model")
        except UpstreamUnavailable as exc:
            return _gateway_error(503, str(exc), anthropic)
        except (ValueError, TypeError) as exc:
            return _gateway_error(400, str(exc), anthropic)
        except Exception as exc:
            log.error("generation failed: provider=%s exception=%s", model.owner if "model" in locals() else "unknown", type(exc).__name__)
            status = getattr(exc, "status_code", 502)
            if not isinstance(status, int) or status < 400 or status > 599:
                status = 502
            message = "CodeBuddy upstream request failed" if "model" in locals() and model.owner == "codebuddy" else str(exc)
            return _gateway_error(status, message, anthropic)
        finally:
            if disconnect_watcher is not None:
                disconnect_watcher.cancel()
            # Streaming responses own their handler until their generator ends.
            if handler is not None and not streaming_handoff:
                await handler.close()

    return app


app = create_app()
