"""Account-scoped, coalesced model discovery."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .auth import ChatGPTAuth, CodeBuddyAuth, Session, UpstreamUnavailable


@dataclass(frozen=True)
class Model:
    id: str
    owner: str
    images: bool = False
    tools: bool = True
    context_window: int = 128_000


class Directory:
    def __init__(self, authorize: Callable[[], Awaitable[Session]], fetch: Callable[[Session], Awaitable[list[Model]]], ttl: float = 300):
        self.authorize = authorize
        self.fetch = fetch
        self.ttl = ttl
        self.cache: dict[str, tuple[float, list[Model]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def list(self) -> tuple[Session, list[Model]]:
        session = await self.authorize()
        key = session.identity
        entry = self.cache.get(key)
        if entry and entry[0] > time.monotonic():
            return session, entry[1]
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self.cache.get(key)
            if entry and entry[0] > time.monotonic():
                return session, entry[1]
            try:
                models = await asyncio.wait_for(self.fetch(session), 20)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, asyncio.TimeoutError) as exc:
                raise UpstreamUnavailable("Upstream model directory is unavailable") from exc
            unique = list({model.id: model for model in reversed(models)}.values())[::-1]
            self.cache[key] = (time.monotonic() + self.ttl, unique)
            return session, unique


def _entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(x, dict) or not isinstance(x.get("id"), str) or not x["id"].strip() for x in value):
        raise ValueError("Invalid model directory entries")
    return value


async def chatgpt_catalog(client: httpx.AsyncClient, session: Session) -> list[Model]:
    response = await client.get(session.base_url + "/models", headers={"Authorization": "Bearer " + session.token, **session.headers}, timeout=20)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Invalid ChatGPT model directory")
    if "models" in body:
        raw = body["models"]
        if not isinstance(raw, list):
            raise ValueError("Invalid ChatGPT model directory")
        entries = _entries([{**item, "id": item.get("slug")} if isinstance(item, dict) else item for item in raw])
    else:
        entries = _entries(body.get("data"))
    return [Model(id=item["id"], owner="openai", images="image" in item.get("input_modalities", []),
                  context_window=item.get("context_window") if isinstance(item.get("context_window"), int) and item["context_window"] > 0 else 128_000)
            for item in entries if item["id"] != "codex-auto-review"]


async def codebuddy_catalog(client: httpx.AsyncClient, session: Session) -> list[Model]:
    response = await client.get(session.base_url + "/enterprises/personal/models",
        headers={"Authorization": "Bearer " + session.token, **session.headers}, timeout=20)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), dict):
        raise ValueError("Invalid CodeBuddy model directory")
    entries = _entries(body["data"].get("models"))
    return [Model(id=item["id"], owner="codebuddy", images=item.get("supportsImages") is True and item.get("disabledMultimodal") is not True,
                  tools=item.get("supportsToolCall") is not False,
                  context_window=item.get("maxInputTokens") if isinstance(item.get("maxInputTokens"), int) and item["maxInputTokens"] > 0 else 128_000)
            for item in entries if "text-to-image" not in item.get("tags", [])]


class ModelCatalog:
    def __init__(self, chatgpt: ChatGPTAuth, codebuddy: CodeBuddyAuth, client: httpx.AsyncClient):
        self.directories = [
            Directory(chatgpt.authorize, lambda session: chatgpt_catalog(client, session)),
            Directory(codebuddy.authorize, lambda session: codebuddy_catalog(client, session)),
        ]

    async def list(self) -> list[Model]:
        results = await asyncio.gather(*(x.list() for x in self.directories), return_exceptions=True)
        success = [models for result in results if not isinstance(result, Exception) for _, models in [result]]
        if not success:
            raise UpstreamUnavailable("Both model directories are unavailable")
        return [model for group in success for model in group]

    async def resolve(self, name: str) -> tuple[Session, Model]:
        explicit = name.startswith("codebuddy/")
        actual = name[len("codebuddy/"):] if explicit else name
        failures: list[Exception] = []
        for directory in self.directories[1:] if explicit else self.directories:
            try:
                session, models = await directory.list()
            except Exception as exc:
                failures.append(exc)
                continue
            model = next((x for x in models if x.id == actual), None)
            if model:
                return session, model
        if failures:
            raise UpstreamUnavailable("Model directory or authorization is unavailable")
        raise LookupError(f"Model '{name}' is not available for this account")
