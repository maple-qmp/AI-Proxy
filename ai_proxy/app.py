"""Local HTTP surface backed by LiteLLM's protocol converters."""

import asyncio
import contextlib
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
from .logstore import LogRecord, LogStore, client_label
from .transport import GatewayTransport, diagnostics_sink

log = logging.getLogger("ai_proxy")
litellm.suppress_debug_info = True
litellm.drop_params = False


def _gateway_error(status: int, message: str, anthropic: bool = False, *, param: str | None = None) -> JSONResponse:
    if anthropic:
        kind = "authentication_error" if status == 401 else "not_found_error" if status == 404 else "invalid_request_error" if status < 500 else "api_error"
        return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)
    return JSONResponse({"error": {"message": message, "type": "authentication_error" if status == 401 else "invalid_request_error" if status < 500 else "server_error", "param": param, "code": None}}, status_code=status)


async def _body(request: Request) -> tuple[dict[str, Any], bytes]:
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
    return value, raw


def _status_of(response: Any) -> int:
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and 100 <= status <= 599 else 500


def _usage(value: Any) -> dict[str, int] | None:
    """Read token counts from either an OpenAI or an Anthropic shaped body."""
    if not isinstance(value, dict):
        return None
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    def pick(*keys: str) -> int | None:
        for key in keys:
            found = usage.get(key)
            if isinstance(found, int) and not isinstance(found, bool):
                return found
        return None
    prompt = pick("prompt_tokens", "input_tokens")
    completion = pick("completion_tokens", "output_tokens")
    total = pick("total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _usage_from_stream(route: str, text: str) -> dict[str, int] | None:
    """Best-effort usage extraction from an SSE transcript.

    Upstream chunks only carry usage when the client asked for it, so a missing
    value is normal and stays NULL rather than becoming a misleading zero.
    """
    found: dict[str, int] | None = None
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message_delta":
            candidate = _usage({"usage": event.get("usage")})
        elif event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            candidate = _usage(event["response"])
        else:
            candidate = _usage(event)
        if candidate is not None:
            found = candidate
    return found


class _Capture:
    """Collects one request's log row without altering the request's control flow."""

    __slots__ = ("store", "limit", "started", "record", "ttfb_ms", "chunks", "chunk_bytes",
                 "done", "diagnostics")

    def __init__(self, store: LogStore | None, limit: int, route: str, request: Request) -> None:
        self.store = store
        self.limit = limit
        self.started = time.monotonic()
        self.diagnostics: dict[str, Any] = {}
        user_agent = request.headers.get("user-agent", "")
        self.record = LogRecord(
            ts=int(time.time() * 1000),
            route=route,
            client=client_label(user_agent),
            client_detail=user_agent[:120] or None,
        )
        self.ttfb_ms: int | None = None
        self.chunks: list[str] = []
        self.chunk_bytes = 0
        self.done = False

    def set_body(self, raw: bytes) -> None:
        text, size, truncated = self._cap(raw)
        self.record.request_body = text
        self.record.request_bytes = size
        self.record.request_truncated = int(truncated)

    def take_response(self, value: Any) -> None:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
        capped, size, truncated = self._cap(text)
        self.record.response_body = capped
        self.record.response_bytes = size
        self.record.response_truncated = int(truncated)
        usage = _usage(value)
        if usage:
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if usage.get(field) is not None:
                    setattr(self.record, field, usage[field])
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            self.record.upstream_request_id = value["id"][:128]

    def append_stream(self, chunk: bytes | str, name: str | None = None) -> None:
        if self.ttfb_ms is None:
            self.ttfb_ms = int((time.monotonic() - self.started) * 1000)
        if self.chunk_bytes >= self.limit:
            self.record.response_truncated = 1
            return
        piece = name + ": " + (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk) \
            if name else (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk)
        self.chunks.append(piece)
        self.chunk_bytes += len(piece)

    def _cap(self, raw: bytes | str) -> tuple[str, int, bool]:
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace")
        size = len(data)
        if size <= self.limit:
            return data.decode("utf-8", "replace"), size, False
        marker = "\n…[truncated %d bytes]" % size
        keep = max(0, self.limit - len(marker.encode("utf-8")))
        return data[:keep].decode("utf-8", "replace") + marker, size, True

    def finish(self, status: int, *, completed: bool = True, error: str | None = None,
               category: str | None = None) -> None:
        """Finalize and enqueue. Never awaits: this runs under cancellation."""
        if self.done:
            return
        self.done = True
        record = self.record
        record.status = status
        record.completed = 1 if completed else 0
        record.duration_ms = int((time.monotonic() - self.started) * 1000)
        record.ttfb_ms = self.ttfb_ms
        if error:
            record.error_text = error[:2000]
            record.error_category = category or "error"
        elif category:
            record.error_category = category
        if self.chunks:
            text = "".join(self.chunks)
            if not record.response_body:
                capped, size, truncated = self._cap(text)
                record.response_body = capped
                record.response_bytes = size
                record.response_truncated = record.response_truncated or int(truncated)
            usage = _usage_from_stream(record.route, text)
            if usage:
                for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if getattr(record, field) is None and usage.get(field) is not None:
                        setattr(record, field, usage[field])
        # Correlation ids come from the transport, never from the response text.
        if self.diagnostics.get("upstream_request_id") and not record.upstream_request_id:
            record.upstream_request_id = str(self.diagnostics["upstream_request_id"])[:128]
        code = self.diagnostics.get("upstream_code")
        if isinstance(code, int) and record.upstream_code is None:
            record.upstream_code = code
        if self.store is not None:
            self.store.submit(record)


def _instrumented(capture: _Capture, inner: AsyncIterator[bytes],
                  handler: AsyncHTTPHandler | None) -> AsyncIterator[bytes]:
    """Tee a streaming response so the log row closes when the stream does."""

    async def wrapper() -> AsyncIterator[bytes]:
        try:
            async for chunk in inner:
                capture.append_stream(chunk)
                yield chunk
            capture.finish(200)
        except asyncio.CancelledError:
            capture.finish(499, completed=False, error="client disconnected", category="cancelled")
            raise
        except GeneratorExit:
            # Starlette closes the generator this way on a plain disconnect.
            capture.finish(200, completed=False, error="client disconnected", category="client_closed")
            raise
        except Exception as exc:
            capture.finish(502, completed=False, error=str(exc)[:500], category="stream_error")
            raise
        finally:
            if handler is not None:
                with contextlib.suppress(Exception):
                    await handler.close()

    return wrapper()


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


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s must be an integer; using %d", name, default)
        return default
    if not low <= value <= high:
        log.warning("%s must be between %d and %d; using %d", name, low, high, default)
        return default
    return value


def _flag_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def create_app(*, client: httpx.AsyncClient | None = None, chatgpt_auth_file: Path | None = None,
               codebuddy_auth_file: Path | None = None, log_db: Path | None = None) -> FastAPI:
    owned_client = client is None
    upstream_client = client or httpx.AsyncClient()
    auth_file = chatgpt_auth_file or (Path(os.environ["GATEWAY_AUTH_FILE"]) if os.getenv("GATEWAY_AUTH_FILE") else default_auth_file())
    cb_file = codebuddy_auth_file or (Path(os.environ["CODEBUDDY_AUTH_FILE"]) if os.getenv("CODEBUDDY_AUTH_FILE") else None)
    chatgpt_auth = ChatGPTAuth(auth_file, upstream_client)
    codebuddy_auth = CodeBuddyAuth(upstream_client, cb_file)
    catalog = ModelCatalog(chatgpt_auth, codebuddy_auth, upstream_client)

    store: LogStore | None = None
    if _flag_env("LOG_ENABLED"):
        db_path = log_db or (Path(os.environ["LOG_DB_PATH"]) if os.getenv("LOG_DB_PATH")
                             else Path.home() / ".config/ai-proxy/logs.db")
        store = LogStore(db_path,
                         max_rows=_int_env("LOG_MAX_ROWS", 5000, 100, 1_000_000),
                         body_limit=_int_env("LOG_BODY_LIMIT_BYTES", 262_144, 1024, 16 * 1024 * 1024),
                         queue_size=_int_env("LOG_QUEUE_SIZE", 256, 8, 10_000))
    body_limit = store.body_limit if store is not None else 262_144
    started_at = time.time()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        prune_task: asyncio.Task[None] | None = None
        if store is not None:
            await store.start()

            async def periodic_prune() -> None:
                while True:
                    await asyncio.sleep(300)
                    try:
                        await store.prune()
                    except Exception:  # noqa: BLE001 - pruning is best effort
                        log.debug("periodic log prune failed", exc_info=True)

            prune_task = asyncio.create_task(periodic_prune())
        try:
            yield
        finally:
            if prune_task is not None:
                prune_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await prune_task
            if store is not None:
                await store.stop()
            if owned_client:
                await upstream_client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.catalog = catalog
    app.state.log_store = store
    app.state.started_at = started_at
    app.state.chatgpt_auth = chatgpt_auth
    app.state.codebuddy_auth = codebuddy_auth

    # Imported here so the dashboard module can import app-level helpers without
    # a circular import at module load time.
    from .dashboard import register_dashboard

    register_dashboard(app, store=store, catalog=catalog, chatgpt_auth=chatgpt_auth,
                       codebuddy_auth=codebuddy_auth, body_limit=body_limit,
                       started_at=started_at)

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        path = request.url.path
        public_page = (path.rstrip("/") == "/dashboard" or path.startswith("/dashboard/assets/"))
        if path == "/healthz" or (public_page and request.method in ("GET", "HEAD")):
            return await call_next(request)
        key = os.getenv("GATEWAY_API_KEY")
        if key:
            bearer = request.headers.get("authorization", "")
            supplied = bearer[7:] if bearer.lower().startswith("bearer ") else request.headers.get("x-api-key", "") if path.startswith("/v1/messages") else ""
            if not hmac.compare_digest(supplied, key):
                if path.startswith("/dashboard/api/"):
                    return JSONResponse({"error": {"message": "Invalid API key"}}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                return _gateway_error(401, "Incorrect API key provided", path.startswith("/v1/messages"))
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
        capture = _Capture(store, body_limit, route, request)
        response: Any = None
        if route not in ("chat/completions", "responses", "messages"):
            response = _gateway_error(404, "Not found", route.startswith("messages"))
            capture.finish(404, error="Not found", category="route_not_found")
            return response
        anthropic = route == "messages"
        handler: AsyncHTTPHandler | None = None
        streaming_handoff = False
        disconnect_watcher: asyncio.Task[None] | None = None
        try:
            payload, raw = await _body(request)
            capture.set_body(raw)
            disconnect_watcher = asyncio.create_task(_cancel_on_disconnect(request, asyncio.current_task()))
            name = payload.get("model")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("model is required")
            capture.record.requested_model = name.strip()[:200]
            session, model = await catalog.resolve(name)
            capture.record.provider = model.owner
            capture.record.model_id = model.id[:200]
            _validate_features(payload, model, route)
            stream = payload.get("stream") is True
            capture.record.stream = int(stream)
            diagnostics: dict[str, Any] = {}
            capture.diagnostics = diagnostics
            diagnostics_sink.set(diagnostics)
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
                        capture.take_response(value)
                        streaming_handoff = True
                        inner = _anthropic_events(value)
                        return StreamingResponse(_instrumented(capture, inner, None),
                                                 media_type="text/event-stream")
                    capture.take_response(value)
                    response = JSONResponse(value)
                    return response
                if stream:
                    async def anthropic_stream() -> AsyncIterator[bytes]:
                        async for event in result:
                            yield event if isinstance(event, bytes) else event.encode() if isinstance(event, str) else _frame(event)
                    streaming_handoff = True
                    response = StreamingResponse(_instrumented(capture, anthropic_stream(), handler),
                                                 media_type="text/event-stream")
                    return response
                await handler.close()
                value = _json(result)
                value["model"] = name
                capture.take_response(value)
                response = JSONResponse(value)
                return response
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
                streaming_handoff = True
                response = StreamingResponse(_instrumented(capture, openai_stream(), handler),
                                             media_type="text/event-stream")
                return response
            await handler.close()
            value = _json(result)
            value["model"] = name
            capture.take_response(value)
            response = JSONResponse(value)
            return response
        except LookupError as exc:
            response = _gateway_error(404, str(exc), anthropic, param="model")
            capture.finish(404, error=str(exc), category="model_not_found")
            return response
        except UpstreamUnavailable as exc:
            response = _gateway_error(503, str(exc), anthropic)
            capture.finish(503, error=str(exc), category="upstream_unavailable")
            return response
        except (ValueError, TypeError) as exc:
            response = _gateway_error(400, str(exc), anthropic)
            capture.finish(400, error=str(exc), category="invalid_request")
            return response
        except Exception as exc:
            log.error("generation failed: provider=%s exception=%s", model.owner if "model" in locals() else "unknown", type(exc).__name__)
            status = getattr(exc, "status_code", 502)
            if not isinstance(status, int) or status < 400 or status > 599:
                status = 502
            message = "CodeBuddy upstream request failed" if "model" in locals() and model.owner == "codebuddy" else str(exc)
            response = _gateway_error(status, message, anthropic)
            capture.finish(status, error=message, category="upstream_error")
            return response
        finally:
            if disconnect_watcher is not None:
                disconnect_watcher.cancel()
            # Non-streaming paths finalize here so a status is recorded even for
            # a return branch added later; the record's own finish() is idempotent.
            if not streaming_handoff and not capture.done:
                capture.finish(_status_of(response) if response is not None else 500)
            # Streaming responses own their handler until their generator ends.
            if handler is not None and not streaming_handoff:
                await handler.close()

    return app


app = create_app()
