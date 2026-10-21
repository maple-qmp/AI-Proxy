import asyncio
import json
import time

import httpx

from ai_proxy.auth import ChatGPTAuth, CodeBuddyAuth
from ai_proxy.catalog import ModelCatalog


def test_catalog_routes_both_backends_and_prefers_chatgpt(tmp_path):
    auth_file = tmp_path / "auth.json"
    cb_file = tmp_path / "cb.info"
    auth_file.write_text(json.dumps({"openai": {"type": "oauth", "access": "token-a", "refresh": "refresh-a", "clientId": "client-a", "expires": time.time() * 1000 + 3600000}}))
    cb_file.write_text(json.dumps({"auth": {"accessToken": "token-b", "refreshToken": "refresh-b", "expiresAt": time.time() * 1000 + 3600000}, "account": {"uid": "u1"}}))
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "shared"}, {"id": "openai-only"}]})
        if request.url.path.endswith("/enterprises/personal/models"):
            return httpx.Response(200, json={"code": 0, "data": {"models": [{"id": "shared"}, {"id": "cb-only"}]}})
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            catalog = ModelCatalog(ChatGPTAuth(auth_file, client), CodeBuddyAuth(client, cb_file), client)
            models = await catalog.list()
            assert [(x.id, x.owner) for x in models] == [("shared", "openai"), ("openai-only", "openai"), ("shared", "codebuddy"), ("cb-only", "codebuddy")]
            assert (await catalog.resolve("shared"))[1].owner == "openai"
            assert (await catalog.resolve("codebuddy/shared"))[1].owner == "codebuddy"
            await catalog.list()
            assert calls.count("/v1/models") == 1
    asyncio.run(run())
