import asyncio
import json
import time

import httpx
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

import ai_proxy.app as gateway
from ai_proxy.auth import Session
from ai_proxy.catalog import Model
from ai_proxy.transport import GatewayTransport


def completion(message=None):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "glm",
            "choices": [{"index": 0, "message": message or {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}}


def build_app(monkeypatch, *, owner="codebuddy", upstream=None, auth_file=None, cb_file=None):
    app = gateway.create_app(chatgpt_auth_file=auth_file, codebuddy_auth_file=cb_file)
    session = Session("identity", "secret", {}, "https://copilot.tencent.com/v2", False)
    model = Model("glm", owner, images=True)

    async def resolve(_name):
        return session, model

    async def listed():
        return [model]

    app.state.catalog.resolve = resolve
    app.state.catalog.list = listed
    monkeypatch.setattr(gateway, "_handler", lambda _model, _session, _diag=None: AsyncHTTPHandler(
        transport=GatewayTransport(
            httpx.MockTransport(upstream or (lambda _req: httpx.Response(200, json=completion()))),
            codebuddy=owner == "codebuddy", subscription=owner == "openai")))
    return app


def call(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await getattr(client, method)(path, **kwargs)
            if app.state.log_store is not None:
                await app.state.log_store.flush()
            return response

    return asyncio.run(run())


def logs_json(app, query=""):
    response = call(app, "get", "/dashboard/api/logs" + query)
    assert response.status_code == 200, response.text
    return response.json()


def test_successful_request_is_recorded_with_bodies(monkeypatch):
    app = build_app(monkeypatch)
    response = call(app, "post", "/v1/chat/completions", json={
        "model": "glm", "messages": [{"role": "user", "content": "你好"}]},
        headers={"user-agent": "python-httpx/0.28.1"})
    assert response.status_code == 200

    page = logs_json(app)
    assert page["total"] == 1
    row = page["items"][0]
    assert row["status"] == 200
    assert row["provider"] == "codebuddy"
    assert row["requested_model"] == "glm"
    assert row["client"] == "httpx"
    assert row["tokens"] == {"prompt": 8, "completion": 3, "total": 11}
    assert row["completed"] is True
    assert "request_preview" in row and "你好" in row["request_preview"]

    detail = call(app, "get", f"/dashboard/api/logs/{row['id']}").json()["log"]
    assert "你好" in detail["bodies"]["request"]
    assert "hi" in detail["bodies"]["response"]


def test_error_paths_record_status_and_category(monkeypatch):
    def upstream(_request):
        return httpx.Response(403, json={"code": 11128, "requestId": "req-9"})

    app = build_app(monkeypatch, upstream=upstream)
    bad = call(app, "post", "/v1/chat/completions", content=b"{not json")
    assert bad.status_code == 400
    missing = call(app, "post", "/v1/chat/completions", json={"model": ""})
    assert missing.status_code == 400
    upstream_error = call(app, "post", "/v1/chat/completions",
                          json={"model": "glm", "messages": [{"role": "user", "content": "hi"}]})
    assert upstream_error.status_code == 403

    page = logs_json(app)
    by_status = {}
    for row in page["items"]:
        by_status.setdefault(row["status"], []).append(row)
    assert 400 in by_status and 403 in by_status
    assert by_status[400][0]["error"]["category"] == "invalid_request"
    assert by_status[403][0]["error"]["category"] == "upstream_error"
    assert by_status[403][0]["provider"] == "codebuddy"


def test_unknown_model_records_404(monkeypatch):
    app = build_app(monkeypatch)

    async def resolve(_name):
        raise LookupError("Model 'nope' is not available for this account")

    app.state.catalog.resolve = resolve
    response = call(app, "post", "/v1/chat/completions",
                    json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 404
    row = logs_json(app)["items"][0]
    assert row["status"] == 404
    assert row["error"]["category"] == "model_not_found"


def test_streaming_record_includes_ttfb_and_completes(monkeypatch):
    def upstream(request):
        body = json.loads(request.content)
        assert body.get("stream") is True
        chunks = [
            {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "glm",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}]},
            {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "glm",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n")

    app = build_app(monkeypatch, upstream=upstream)
    response = call(app, "post", "/v1/chat/completions", json={
        "model": "glm", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert "[DONE]" in response.text

    row = logs_json(app)["items"][0]
    assert row["stream"] is True
    assert row["completed"] is True
    assert row["status"] == 200
    assert row["ttfb_ms"] is not None
    assert row["duration_ms"] >= row["ttfb_ms"]

    detail = call(app, "get", f"/dashboard/api/logs/{row['id']}").json()["log"]
    assert "data:" in detail["bodies"]["response"]


def test_streaming_client_disconnect_marks_incomplete(monkeypatch):
    from ai_proxy.app import _Capture, _instrumented

    captured = []

    class Store:
        def submit(self, record):
            captured.append(record)

    async def main():
        class Request:
            headers = {"user-agent": "claude-cli/1.0"}

        capture = _Capture(Store(), 1024, "messages", Request())

        async def inner():
            yield b"event: message_start\ndata: {}\n\n"
            yield b"event: message_delta\ndata: {}\n\n"

        stream = _instrumented(capture, inner(), None)
        await stream.__anext__()
        await stream.aclose()

    asyncio.run(main())
    assert len(captured) == 1
    record = captured[0]
    assert record.completed == 0
    assert record.error_category in ("client_closed", "cancelled")
    assert record.duration_ms >= 0


def test_filters_and_validation(monkeypatch):
    app = build_app(monkeypatch)
    call(app, "post", "/v1/chat/completions", json={
        "model": "glm", "messages": [{"role": "user", "content": "hello"}]})

    assert logs_json(app, "?status=success")["total"] == 1
    assert logs_json(app, "?status=error")["total"] == 0
    assert logs_json(app, "?provider=codebuddy")["total"] == 1
    assert logs_json(app, "?provider=openai")["total"] == 0
    assert logs_json(app, "?q=hello")["total"] == 0  # body search is opt-in
    assert logs_json(app, "?q=hello&body=1")["total"] == 1

    bad = call(app, "get", "/dashboard/api/logs?window=nope")
    assert bad.status_code == 400
    assert "window" in bad.json()["error"]["message"]


def test_delete_logs(monkeypatch):
    app = build_app(monkeypatch)
    call(app, "post", "/v1/chat/completions", json={
        "model": "glm", "messages": [{"role": "user", "content": "hi"}]})
    removed = call(app, "delete", "/dashboard/api/logs")
    assert removed.status_code == 200
    assert removed.json()["deleted"] == 1
    assert logs_json(app)["total"] == 0


def test_status_endpoint(monkeypatch):
    app = build_app(monkeypatch)
    payload = call(app, "get", "/dashboard/api/status").json()
    assert payload["ok"] is True
    assert payload["features"]["logs"] is True
    assert payload["store"]["enabled"] is True
    assert payload["store"]["max_rows"] == 5000


def test_accounts_never_leak_secrets(tmp_path, monkeypatch):
    now = time.time() * 1000
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"openai": {
        "type": "oauth", "access": "SECRET-ACCESS-TOKEN", "refresh": "SECRET-REFRESH-TOKEN",
        "clientId": "SECRET-CLIENT-ID", "expires": now + 3_600_000,
        "scopes": ["chatgpt.tokens.use.direct"]}}))
    cb_dir = tmp_path / "cb"
    cb_dir.mkdir()
    cb_file = cb_dir / "workbuddy-desktop.info"
    cb_file.write_text(json.dumps({
        "auth": {"accessToken": "SECRET-CB-ACCESS", "refreshToken": "SECRET-CB-REFRESH",
                 "expiresAt": now + 3_600_000, "domain": "www.codebuddy.cn"},
        "account": {"uid": "u1", "nickname": "测试账号"}}))

    app = build_app(monkeypatch, auth_file=auth_file, cb_file=cb_file)
    response = call(app, "get", "/dashboard/api/accounts")
    assert response.status_code == 200
    text = response.text
    for secret in ("SECRET-ACCESS-TOKEN", "SECRET-REFRESH-TOKEN", "SECRET-CLIENT-ID",
                   "SECRET-CB-ACCESS", "SECRET-CB-REFRESH"):
        assert secret not in text

    payload = response.json()["accounts"]
    openai = payload["openai"]
    assert openai["state"] == "ok"
    assert openai["kind"] == "oauth"
    assert openai["account_id"] and len(openai["account_id"]) == 12
    assert "access" not in openai and "refresh" not in openai
    codebuddy = payload["codebuddy"]
    assert codebuddy["credentials"][0]["nickname"] == "测试账号"
    assert codebuddy["credentials"][0]["app"] == "workbuddy"
    assert codebuddy["selected_id"] == codebuddy["credentials"][0]["id"]


def test_accounts_missing_files_use_constant_messages(tmp_path, monkeypatch):
    app = build_app(monkeypatch, auth_file=tmp_path / "absent.json",
                    cb_file=tmp_path / "absent.info")
    response = call(app, "get", "/dashboard/api/accounts")
    payload = response.json()["accounts"]
    assert payload["openai"]["state"] == "missing"
    assert payload["openai"]["error"] == "未登录 ChatGPT，请运行 python -m ai_proxy login"
    assert payload["codebuddy"]["state"] in ("missing", "invalid")
    # The credential path is shown deliberately, but the error text must be a
    # constant: str(exc) would embed the filesystem path from _read_json.
    assert "absent" not in (payload["openai"]["error"] or "")
    assert "absent" not in (payload["codebuddy"]["error"] or "")


def test_accounts_refresh_reports_failure_without_leaking(monkeypatch):
    app = build_app(monkeypatch)
    response = call(app, "post", "/dashboard/api/accounts/refresh")
    assert response.status_code == 200
    results = response.json()["results"]
    assert results["openai"]["ok"] is False
    assert results["codebuddy"]["ok"] is False
    assert "token" not in json.dumps(results).lower() or "Token" in json.dumps(results)
    assert "access" not in response.text.lower()


def test_models_groups_report_each_provider(monkeypatch):
    from ai_proxy.catalog import ModelCatalog

    class Directory:
        ttl = 300

        def __init__(self, models=None, error=None):
            self.models = models or []
            self.error = error
            self.cache = {}

        async def authorize(self):
            if self.error:
                from ai_proxy.auth import UpstreamUnavailable
                raise UpstreamUnavailable("nope")
            return type("S", (), {"identity": "id"})()

        async def list(self):
            session = await self.authorize()
            if self.error:
                from ai_proxy.auth import UpstreamUnavailable
                raise UpstreamUnavailable("nope")
            return session, self.models

    from ai_proxy.catalog import Model

    app = build_app(monkeypatch)
    app.state.catalog.directories = [
        Directory([Model("gpt-6.1-sol", "openai")]),
        Directory(error=True),
    ]
    response = call(app, "get", "/dashboard/api/models")
    assert response.status_code == 200
    groups = {g["provider"]: g for g in response.json()["groups"]}
    assert groups["openai"]["status"]["ok"] is True
    assert groups["openai"]["models"][0]["route_name"] == "gpt-6.1-sol"
    assert groups["codebuddy"]["status"]["ok"] is False
    assert groups["codebuddy"]["status"]["error"]
    assert groups["codebuddy"]["models"] == []


def test_models_shadowing_flag(monkeypatch):
    from ai_proxy.catalog import Model

    class Directory:
        ttl = 300

        def __init__(self, models):
            self.models = models
            self.cache = {}

        async def authorize(self):
            return type("S", (), {"identity": "id"})()

        async def list(self):
            return await self.authorize(), self.models

    app = build_app(monkeypatch)
    app.state.catalog.directories = [
        Directory([Model("shared", "openai")]),
        Directory([Model("shared", "codebuddy")]),
    ]
    payload = call(app, "get", "/dashboard/api/models").json()
    openai_group, codebuddy_group = payload["groups"]
    assert openai_group["models"][0]["shadowed"] is False
    assert codebuddy_group["models"][0]["shadowed"] is True
    assert codebuddy_group["models"][0]["route_name"] == "codebuddy/shared"


def test_dashboard_page_is_public_but_api_requires_key(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "sekret")
    app = build_app(monkeypatch)

    page = call(app, "get", "/dashboard")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-cache"
    css = call(app, "get", "/dashboard/assets/dashboard.css")
    assert css.status_code == 200

    denied = call(app, "get", "/dashboard/api/logs")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    assert denied.json()["error"]["message"] == "Invalid API key"

    allowed = call(app, "get", "/dashboard/api/logs", headers={"Authorization": "Bearer sekret"})
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"

    # The /v1 surface is untouched by the dashboard whitelist.
    assert call(app, "get", "/v1/models").status_code == 401
    assert call(app, "get", "/healthz").status_code == 200


def test_refresh_rejects_cross_origin(monkeypatch):
    app = build_app(monkeypatch)
    response = call(app, "post", "/dashboard/api/accounts/refresh",
                    headers={"Origin": "https://evil.example", "Host": "localhost:8787"})
    assert response.status_code == 403


def test_logging_disabled_still_serves_dashboard(monkeypatch):
    monkeypatch.setenv("LOG_ENABLED", "0")
    app = build_app(monkeypatch)
    response = call(app, "post", "/v1/chat/completions", json={
        "model": "glm", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200

    payload = call(app, "get", "/dashboard/api/status").json()
    assert payload["features"]["logs"] is False
    page = call(app, "get", "/dashboard/api/logs").json()
    assert page["items"] == []
    assert page["total"] == 0
    assert call(app, "get", "/dashboard/api/logs/1").status_code == 503


def test_body_limit_truncates_end_to_end(monkeypatch):
    monkeypatch.setenv("LOG_BODY_LIMIT_BYTES", "2048")
    app = build_app(monkeypatch)
    call(app, "post", "/v1/chat/completions", json={
        "model": "glm", "messages": [{"role": "user", "content": "长" * 4000}]})

    row = logs_json(app)["items"][0]
    assert row["bodies"]["request_truncated"] is True
    detail = call(app, "get", f"/dashboard/api/logs/{row['id']}").json()["log"]
    assert "truncated" in detail["bodies"]["request"]
    assert len(detail["bodies"]["request"].encode()) <= 2048
