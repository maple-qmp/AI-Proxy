import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ai_proxy import __main__ as cli
from ai_proxy.app import create_app
from ai_proxy.auth import ChatGPTAuth, CodeBuddyAuth, default_auth_file
from ai_proxy.catalog import ModelCatalog
from ai_proxy.login import login_chatgpt


def test_browser_login_callback_refresh_and_relogin(tmp_path):
    path = tmp_path / "auth.json"
    urls = []
    requests = []
    callback_statuses = []
    callback_tasks = []

    def upstream(request):
        if request.url.path == "/v1/models":
            assert request.headers["Authorization"] == "Bearer access-2"
            return httpx.Response(200, json={"data": [{"id": "chatgpt-model"}]})
        requests.append(dict(httpx.QueryParams(request.content.decode())))
        if requests[-1]["grant_type"] == "authorization_code":
            return httpx.Response(200, json={"access_token": "access-" + str(len(urls)),
                "refresh_token": "refresh-" + str(len(urls)), "id_token": "identity",
                "scope": "openid chatgpt.tokens.use.direct", "expires_in": 3600})
        return httpx.Response(200, json={"access_token": "refreshed", "refresh_token": "rotated",
            "scope": "openid chatgpt.tokens.use.direct", "expires_in": 3600})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            def browser(url):
                urls.append(url)
                async def callback():
                    params = parse_qs(urlsplit(url).query)
                    assert params["client_id"] == ["dynamic_agent_client"]
                    assert params["resource"] == ["https://api.openai.com/v1"]
                    assert "chatgpt.tokens.use.direct" in params["scope"][0]
                    assert params["code_challenge_method"] == ["S256"]
                    assert params["ext_agent_host_id"][0].startswith("urn:uuid:")
                    redirect = params["redirect_uri"][0]
                    async with httpx.AsyncClient() as local:
                        bad = await local.get(redirect, params={"state": "wrong", "code": "bad", "client_id": "issued"})
                        callback_statuses.append(bad.status_code)
                        good = await local.get(redirect, params={"state": params["state"][0], "code": "valid", "client_id": "issued"})
                        callback_statuses.append(good.status_code)
                callback_tasks.append(asyncio.create_task(callback()))

            for _ in range(2):
                async def idle_manual():
                    await asyncio.Future()
                await login_chatgpt(path, client=client, port=0, open_browser=browser,
                    manual_url=idle_manual)
            assert urls[0] != urls[1]
            assert parse_qs(urlsplit(urls[0]).query)["ext_agent_host_id"] == parse_qs(urlsplit(urls[1]).query)["ext_agent_host_id"]
            assert (await ChatGPTAuth(path, client).authorize()).token == "access-2"
            catalog = ModelCatalog(ChatGPTAuth(path, client), CodeBuddyAuth(client, tmp_path / "missing.info"), client)
            assert [(item.id, item.owner) for item in await catalog.list()] == [("chatgpt-model", "openai")]
            stored = json.loads(path.read_text())
            stored["openai"]["expires"] = 1
            path.write_text(json.dumps(stored))
            assert (await ChatGPTAuth(path, client).authorize()).token == "refreshed"
            await asyncio.gather(*callback_tasks)

    asyncio.run(run())
    assert callback_statuses == [400, 200, 400, 200]
    assert requests[0]["client_id"] == "issued"
    assert requests[0]["code"] == "valid"
    assert requests[0]["code_verifier"]
    assert requests[-1]["grant_type"] == "refresh_token"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / ".ai-proxy-device-id").st_mode & 0o777 == 0o600


def test_manual_callback_and_failed_grant_do_not_save(tmp_path):
    path = tmp_path / "auth.json"
    seen = []

    def browser(url):
        seen.append(url)

    async def manual():
        while not seen:
            await asyncio.sleep(0)
        params = parse_qs(urlsplit(seen[-1]).query)
        return params["redirect_uri"][0] + "?state=" + params["state"][0] + "&code=manual&client_id=issued"

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "access_token": "access", "refresh_token": "refresh", "id_token": "identity",
            "scope": "openid", "expires_in": 3600}))) as client:
            with pytest.raises(ValueError, match="direct-token scope"):
                await login_chatgpt(path, client=client, port=0, open_browser=browser, manual_url=manual)
        assert not path.exists()
        assert (tmp_path / ".ai-proxy-device-id").exists()

    asyncio.run(run())


def test_callback_survives_client_disconnect(tmp_path):
    """A browser that hangs up after sending the code must not lose the login."""
    path = tmp_path / "auth.json"
    urls = []

    def browser(url):
        urls.append(url)

        async def go():
            params = parse_qs(urlsplit(url).query)
            host, port = urlsplit(params["redirect_uri"][0]).netloc.split(":")
            reader, writer = await asyncio.open_connection(host, int(port))
            writer.write(f"GET /auth/callback?state={params['state'][0]}&code=code&client_id=issued HTTP/1.1\r\n"
                         "Host: 127.0.0.1\r\n\r\n".encode())
            await writer.drain()
            writer.close()  # leave before the response is written
        asyncio.create_task(go())

    async def idle_manual():
        await asyncio.Future()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "access_token": "access", "refresh_token": "refresh", "id_token": "identity",
            "scope": "openid chatgpt.tokens.use.direct", "expires_in": 3600}))) as client:
            await asyncio.wait_for(login_chatgpt(path, client=client, port=0, open_browser=browser,
                manual_url=idle_manual), 5)
        assert json.loads(path.read_text())["openai"]["access"] == "access"

    asyncio.run(run())


def test_manual_entry_retries_blank_and_malformed_input(tmp_path):
    path = tmp_path / "auth.json"
    urls, reads = [], []

    def browser(url):
        urls.append(url)

    async def manual():
        reads.append(None)
        while not urls:
            await asyncio.sleep(0)
        if len(reads) == 1:
            return ""  # stray ENTER while the browser is still working
        if len(reads) == 2:
            return "http://127.0.0.1:1/auth/callback?state=stale&code=old&client_id=old"
        params = parse_qs(urlsplit(urls[-1]).query)
        return params["redirect_uri"][0] + "?state=" + params["state"][0] + "&code=manual&client_id=issued"

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "access_token": "access", "refresh_token": "refresh", "id_token": "identity",
            "scope": "openid chatgpt.tokens.use.direct", "expires_in": 3600}))) as client:
            await asyncio.wait_for(login_chatgpt(path, client=client, port=0, open_browser=browser,
                manual_url=manual), 5)
        assert len(reads) == 3
        assert json.loads(path.read_text())["openai"]["access"] == "access"

    asyncio.run(run())


def test_closed_stdin_fails_instead_of_hanging(tmp_path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as client:
            with pytest.raises(ValueError, match="Standard input closed"):
                await asyncio.wait_for(login_chatgpt(tmp_path / "auth.json", client=client, port=0,
                    open_browser=lambda _: None, manual_url=lambda: asyncio.sleep(0, result=None)), 5)
            assert not (tmp_path / "auth.json").exists()

    asyncio.run(run())


def test_own_default_paths_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("SERVICE_ENV_FILE", raising=False)
    monkeypatch.delenv("GATEWAY_AUTH_FILE", raising=False)
    config = tmp_path / ".config/ai-proxy"
    config.mkdir(parents=True)
    (config / "service.env").write_text("GATEWAY_AUTH_FILE=/custom/ai-proxy-auth.json\n")
    cli.load_service_env()
    assert os.environ["GATEWAY_AUTH_FILE"] == "/custom/ai-proxy-auth.json"
    assert default_auth_file() == config / "auth.json"
    app = create_app()
    assert app.state.catalog.directories[0].authorize.__self__.path == Path("/custom/ai-proxy-auth.json")
    monkeypatch.setenv("SERVICE_ENV_FILE", str(tmp_path / "override.env"))
    monkeypatch.delenv("GATEWAY_AUTH_FILE")
    (tmp_path / "override.env").write_text("GATEWAY_AUTH_FILE=/override/auth.json\n")
    cli.load_service_env()
    assert os.environ["GATEWAY_AUTH_FILE"] == "/override/auth.json"
    app = create_app()
    assert app.state.catalog.directories[0].authorize.__self__.path == Path("/override/auth.json")
    monkeypatch.delenv("GATEWAY_AUTH_FILE")
    app = create_app()
    assert app.state.catalog.directories[0].authorize.__self__.path == config / "auth.json"
