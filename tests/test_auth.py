import asyncio
import json
import os
import time

import httpx

from ai_proxy.auth import ChatGPTAuth, CodeBuddyAuth


def test_chatgpt_refresh_reuses_original_client_and_file(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"openai": {"type": "oauth", "access": "old", "refresh": "old-refresh",
        "clientId": "issued-client", "expires": 1}, "other": {"keep": True}}))
    calls = []

    def upstream(request):
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "new-refresh",
            "scope": "openid chatgpt.tokens.use.direct", "expires_in": 3600})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            auth = ChatGPTAuth(path, client)
            first, second = await asyncio.gather(auth.authorize(), auth.authorize())
            assert first.token == second.token == "new"
    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0]["client_id"] == "issued-client"
    assert calls[0]["resource"] == "https://api.openai.com/v1"
    assert json.loads(path.read_text())["other"] == {"keep": True}
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_codebuddy_refresh_preserves_desktop_data(tmp_path):
    path = tmp_path / "cb.info"
    path.write_text(json.dumps({"auth": {"accessToken": "old", "refreshToken": "refresh", "expiresAt": 1},
        "account": {"uid": "u1", "enterpriseId": "e1"}, "settings": {"keep": True}}))
    seen = []

    def upstream(request):
        seen.append(request.headers)
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "new", "refreshToken": "new-refresh", "expiresIn": 3600}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            session = await CodeBuddyAuth(client, path).authorize()
            assert session.token == "new"
            assert session.headers["X-Enterprise-Id"] == "e1"
    asyncio.run(run())
    assert seen[0]["X-Refresh-Token"] == "refresh"
    assert json.loads(path.read_text())["settings"] == {"keep": True}
    assert os.stat(path).st_mode & 0o777 == 0o600
