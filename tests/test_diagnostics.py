import asyncio
import json
import logging

import httpx

from ai_proxy.transport import GatewayTransport


def test_codebuddy_11128_logs_only_safe_fields(caplog):
    def upstream(_request):
        return httpx.Response(403, json={"code": 11128, "requestId": "req-123", "message": "SECRET_PROMPT"})

    async def run():
        async with httpx.AsyncClient(transport=GatewayTransport(httpx.MockTransport(upstream), codebuddy=True, debug=True)) as client:
            response = await client.post("https://copilot.tencent.com/v2/chat/completions", json={
                "model": "SECRET_MODEL", "messages": [{"role": "system", "content": "SECRET_PROMPT"}]})
            assert response.status_code == 403

    with caplog.at_level(logging.INFO, logger="ai_proxy.codebuddy"):
        asyncio.run(run())
    text = caplog.text
    assert "upstream_rejected_11128" in text
    assert "req-123" in text
    assert "SECRET_PROMPT" not in text
    assert "SECRET_MODEL" not in text
