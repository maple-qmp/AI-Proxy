"""Local status dashboard: request logs, model catalog and account state.

The page itself is a static single-file app served without an API key so the
browser can load it and ask for one; every ``/dashboard/api/*`` endpoint stays
behind the gateway's normal Bearer auth.
"""

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import ChatGPTAuth, CodeBuddyAuth, UpstreamUnavailable, _jwt_identity, _read_json
from .catalog import Directory, Model, ModelCatalog
from .logstore import LogStore

log = logging.getLogger("ai_proxy.dashboard")

STATIC_DIR = Path(__file__).parent / "static" / "dashboard"

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'none'")

WINDOWS: dict[str, int] = {"1h": 3_600_000, "24h": 86_400_000, "7d": 604_800_000, "all": 0}

# Bucket width per window, so every chart lands in a readable 24-50 bar range.
BUCKETS: dict[str, int] = {"1h": 60_000, "24h": 3_600_000, "7d": 21_600_000, "all": 86_400_000}

# Constant messages: never str(exc), which can embed a credential file path.
ACCOUNT_ERRORS = {
    "openai": "未登录 ChatGPT，请运行 python -m ai_proxy login",
    "codebuddy": "CodeBuddy 未登录，请在桌面端登录",
}
REFRESH_ERRORS = {
    "openai": "ChatGPT Token 刷新失败，请重新登录",
    "codebuddy": "CodeBuddy Token 刷新失败，请在桌面端重新登录",
}
MODEL_ERROR = "模型目录不可用，请检查登录状态"
INVALID_CREDENTIAL = "凭据结构不受网关支持"
PROVIDER_LABELS = {"openai": "ChatGPT", "codebuddy": "CodeBuddy / WorkBuddy"}
PROVIDER_ORDER = ("openai", "codebuddy")


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message}}, status_code=status)


def _ms(moment: float) -> int:
    return int(moment * 1000)


def _display_path(path: Path) -> str:
    try:
        home = Path.home()
        if path.is_relative_to(home):
            return "~/" + str(path.relative_to(home))
    except (OSError, ValueError):
        pass
    return str(path)


def _state(expires_ms: Any, now_ms: int) -> str:
    if not isinstance(expires_ms, (int, float)) or isinstance(expires_ms, bool):
        return "invalid"
    if expires_ms <= now_ms:
        return "expired"
    if expires_ms <= now_ms + 60_000:
        return "expiring"
    return "ok"


def _seconds_left(expires_ms: Any, now_ms: int) -> int | None:
    if not isinstance(expires_ms, (int, float)) or isinstance(expires_ms, bool):
        return None
    return int((expires_ms - now_ms) / 1000)


def _short_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _file_mtime(path: Path) -> int | None:
    try:
        return _ms(path.stat().st_mtime)
    except OSError:
        return None


def _app_kind(filename: str) -> str:
    lowered = filename.lower()
    if "workbuddy" in lowered:
        return "workbuddy"
    if "codebuddy" in lowered or "coding-copilot" in lowered:
        return "codebuddy"
    return "unknown"


# --------------------------------------------------------------------- accounts


def chatgpt_status(path: Path, now_ms: int) -> dict[str, Any]:
    """File-derived ChatGPT state. Returns no token material of any kind."""
    status: dict[str, Any] = {
        "provider": "openai",
        "label": PROVIDER_LABELS["openai"],
        "configured": path.exists(),
        "file": _display_path(path),
        "file_mtime": _file_mtime(path),
        "kind": None,
        "state": "missing",
        "expires_at": None,
        "expires_in_seconds": None,
        "refreshable": False,
        "base_url": "https://api.openai.com/v1",
        "scopes": [],
        "account_id": None,
        "error": None if path.exists() else ACCOUNT_ERRORS["openai"],
    }
    if not path.exists():
        return status
    try:
        data = _read_json(path)
    except UpstreamUnavailable:
        status["state"] = "invalid"
        status["error"] = ACCOUNT_ERRORS["openai"]
        return status
    credential = data.get("openai")
    if not isinstance(credential, dict):
        status["state"] = "invalid"
        status["error"] = ACCOUNT_ERRORS["openai"]
        return status
    kind = credential.get("type")
    status["kind"] = kind if kind in ("oauth", "api_key") else None
    if isinstance(credential.get("baseUrl"), str) and credential["baseUrl"].strip():
        status["base_url"] = credential["baseUrl"].strip().rstrip("/")
    if kind == "api_key":
        key = credential.get("key")
        status.update(state="ok", account_id=_short_id(key) if isinstance(key, str) else None,
                      refreshable=False)
        return status
    if kind != "oauth":
        status["state"] = "invalid"
        status["error"] = ACCOUNT_ERRORS["openai"]
        return status
    scopes = credential.get("scopes")
    if isinstance(scopes, list):
        status["scopes"] = [x for x in scopes if isinstance(x, str)][:20]
    access, refresh = credential.get("access"), credential.get("refresh")
    identity_source = None
    if isinstance(access, str) and access:
        identity_source = _jwt_identity(access)
    if identity_source is None and isinstance(refresh, str) and refresh:
        identity_source = refresh
    status["account_id"] = _short_id(identity_source) if identity_source else None
    status["refreshable"] = isinstance(refresh, str) and bool(refresh)
    if not (isinstance(access, str) and access) or not (isinstance(refresh, str) and refresh):
        status["state"] = "invalid"
        status["error"] = ACCOUNT_ERRORS["openai"]
        return status
    expires = credential.get("expires")
    status["expires_at"] = int(expires) if isinstance(expires, (int, float)) and not isinstance(expires, bool) else None
    status["expires_in_seconds"] = _seconds_left(expires, now_ms)
    status["state"] = _state(expires, now_ms)
    return status


def codebuddy_status(credentials_source: CodeBuddyAuth, selected: Path | None, now_ms: int) -> dict[str, Any]:
    """Every discoverable CodeBuddy/WorkBuddy credential, plus which one wins."""
    candidates = credentials_source.candidates()
    credentials: list[dict[str, Any]] = []
    for path in candidates[:20]:
        entry: dict[str, Any] = {
            "id": _short_id(path.name),
            "app": _app_kind(path.name),
            "file_mtime": _file_mtime(path),
            "state": "invalid",
            "expires_at": None,
            "expires_in_seconds": None,
            "refresh_expires_at": None,
            "refreshable": False,
            "nickname": None,
            "uid": None,
            "detail": None,
            "selected": False,
        }
        try:
            data, auth, account = CodeBuddyAuth._validated(path)
        except UpstreamUnavailable:
            # Show the file (it may be a newer app build the gateway cannot read)
            # but never imply it is usable.
            entry["detail"] = INVALID_CREDENTIAL
            credentials.append(entry)
            continue
        expires = auth.get("expiresAt")
        entry["expires_at"] = int(expires) if isinstance(expires, (int, float)) and not isinstance(expires, bool) else None
        entry["expires_in_seconds"] = _seconds_left(expires, now_ms)
        refresh_expires = auth.get("refreshExpiresAt")
        entry["refresh_expires_at"] = int(refresh_expires) if isinstance(refresh_expires, (int, float)) and not isinstance(refresh_expires, bool) else None
        entry["refreshable"] = isinstance(auth.get("refreshToken"), str) and bool(auth.get("refreshToken"))
        entry["state"] = _state(expires, now_ms)
        nickname = account.get("nickname")
        if isinstance(nickname, str) and nickname.strip():
            entry["nickname"] = nickname.strip()[:64]
        uid = account.get("uid")
        entry["uid"] = str(uid)[:64] if isinstance(uid, (str, int)) and not isinstance(uid, bool) else None
        credentials.append(entry)
    # "Current" means the file the gateway actually reads: newest-first among the
    # files that pass validation. Mark that one, not merely the newest.
    selected_id = _short_id(selected.name) if selected is not None else None
    for item in credentials:
        item["selected"] = item["id"] == selected_id
    usable = [item for item in credentials if item["state"] in ("ok", "expiring")]
    return {
        "provider": "codebuddy",
        "label": PROVIDER_LABELS["codebuddy"],
        "configured": bool(credentials),
        "directory": _display_path(credentials_source.file or credentials_source.directory),
        "selected_id": selected_id,
        "state": (usable[0]["state"] if usable else
                  "expired" if any(x["state"] == "expired" for x in credentials)
                  else "missing" if not credentials else "invalid"),
        "credentials": credentials,
        "error": None if usable else ACCOUNT_ERRORS["codebuddy"],
    }


def accounts_payload(chatgpt: ChatGPTAuth, codebuddy: CodeBuddyAuth) -> dict[str, Any]:
    now_ms = _ms(time.time())
    return {
        "checked_at": now_ms,
        "accounts": {
            "openai": chatgpt_status(chatgpt.path, now_ms),
            "codebuddy": codebuddy_status(codebuddy, codebuddy.selected_file(), now_ms),
        },
    }


# ----------------------------------------------------------------------- models


def _model_payload(model: Model, provider: str, shadowed: bool) -> dict[str, Any]:
    return {
        "id": model.id,
        "route_name": f"codebuddy/{model.id}" if provider == "codebuddy" else model.id,
        "context_window": model.context_window,
        "images": model.images,
        "tools": model.tools,
        "shadowed": shadowed,
    }


def _freshest(directory: Directory) -> tuple[float, list[Model]] | None:
    """The newest cache entry, used when a live fetch fails."""
    entries = [(expiry - directory.ttl, models) for expiry, models in directory.cache.values()]
    return max(entries, key=lambda item: item[0]) if entries else None


def _cache_times(directory: Directory, expiry: float, ttl: float | None = None) -> tuple[int, int]:
    """Translate a monotonic cache expiry into wall-clock ms.

    ``Directory.cache`` is keyed on ``time.monotonic()`` because it only needs
    relative comparisons. The dashboard needs absolute times, so derive them
    from the remaining interval measured now.
    """
    span = directory.ttl if ttl is None else ttl
    remaining_ms = max(0, int((expiry - time.monotonic()) * 1000))
    expires_at = _ms(time.time()) + remaining_ms
    return expires_at - int(span * 1000), expires_at


async def _group(directory: Directory, provider: str, refresh: bool) -> dict[str, Any]:
    source = "cache"
    if refresh:
        try:
            session = await directory.authorize()
            directory.cache.pop(session.identity, None)
            source = "live"
        except UpstreamUnavailable:
            pass
    try:
        session, models = await directory.list()
    except UpstreamUnavailable:
        stale = _freshest(directory)
        cached_at, _ = _cache_times(directory, stale[0]) if stale else (None, None)
        return {
            "provider": provider,
            "label": PROVIDER_LABELS[provider],
            "status": {"ok": stale is not None, "source": "stale" if stale else "none",
                       "cached_at": cached_at, "expires_at": None,
                       "error": None if stale else MODEL_ERROR},
            "models": [_model_payload(model, provider, False) for model in (stale[1] if stale else [])],
        }
    entry = directory.cache.get(session.identity)
    cached_at, expires_at = _cache_times(directory, entry[0]) if entry else (None, None)
    return {
        "provider": provider,
        "label": PROVIDER_LABELS[provider],
        "status": {"ok": True, "source": source, "cached_at": cached_at,
                   "expires_at": expires_at, "error": None},
        "models": [_model_payload(model, provider, False) for model in models],
    }


async def models_payload(catalog: ModelCatalog, refresh: bool) -> dict[str, Any]:
    directories = catalog.directories
    groups = await asyncio.gather(
        *(_group(directory, provider, refresh) for directory, provider in zip(directories, PROVIDER_ORDER)),
        return_exceptions=True,
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider, group in zip(PROVIDER_ORDER, groups):
        if isinstance(group, Exception):
            log.warning("model group %s failed: %s", provider, type(group).__name__)
            group = {"provider": provider, "label": PROVIDER_LABELS[provider],
                     "status": {"ok": False, "source": "none", "cached_at": None,
                                "expires_at": None, "error": MODEL_ERROR},
                     "models": []}
        for model in group["models"]:
            if model["id"] in seen:
                model["shadowed"] = True
            else:
                seen.add(model["id"])
        result.append(group)
    return {"refreshed": refresh, "groups": result,
            "generated_at": _ms(time.time()),
            "total": sum(len(group["models"]) for group in result)}


# ------------------------------------------------------------------------ router


def register_dashboard(app: FastAPI, *, store: LogStore | None, catalog: ModelCatalog,
                       chatgpt_auth: ChatGPTAuth, codebuddy_auth: CodeBuddyAuth,
                       body_limit: int, started_at: float) -> None:
    if not _enabled():
        return
    router = APIRouter(prefix="/dashboard", include_in_schema=False)
    refresh_lock = asyncio.Lock()
    last_refresh = {"at": 0.0}

    @app.middleware("http")
    async def dashboard_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/dashboard"):
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Content-Security-Policy", CSP)
            if request.url.path.startswith("/dashboard/api/"):
                response.headers.setdefault("Cache-Control", "no-store")
            else:
                # The page and its assets must pick up a UI update on reload;
                # this is a local console, so revalidation costs nothing.
                response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @router.get("")
    @router.get("/")
    async def page() -> Any:
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            return JSONResponse({"error": {"message": "dashboard not installed"}}, status_code=404)
        return FileResponse(index, media_type="text/html")

    @router.get("/api/status")
    async def status() -> dict[str, Any]:
        host = "127.0.0.1"
        try:
            import os
            host = os.getenv("HOST", host)
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "uptime_s": int(time.time() - started_at),
            "host": host,
            "features": {"logs": store is not None, "dashboard": True},
            "store": (await store.info()) if store is not None else {
                "enabled": False, "path": "", "size_bytes": 0, "rows": 0,
                "max_rows": 0, "body_limit": body_limit, "dropped": 0,
                "failed_batches": 0, "error": "logging disabled",
            },
        }

    @router.get("/api/logs")
    async def logs(
        window: str = "24h",
        status: str = "",
        provider: str = "",
        route: str = "",
        client: str = "",
        model: str = "",
        q: str = "",
        body: int = 0,
        limit: int = 25,
        offset: int = 0,
    ) -> Any:
        span = WINDOWS.get(window)
        if span is None:
            return _err(400, "window 必须是 1h、24h、7d 或 all")
        until = _ms(time.time())
        if span:
            since = until - span
        else:
            earliest = (await store.earliest()) if store is not None else None
            since = earliest if earliest is not None else 0
        if store is None:
            return {"items": [], "total": 0, "limit": limit, "offset": offset,
                    "summary": {"total": 0, "success": 0, "errors": 0, "error_rate": 0.0,
                                "streams": 0, "incomplete": 0,
                                "latency_ms": {"avg": 0, "p50": 0, "p95": 0},
                                "tokens": {"prompt": 0, "completion": 0, "total": 0}},
                    "histogram": [], "by_provider": [], "top_models": [],
                    "facets": {"providers": [], "routes": [], "clients": [], "models": []}}
        filters = {
            "since": since, "until": until,
            "status": status or None,
            "provider": provider or None, "route": route or None,
            "client": client or None, "model": model or None,
            "q": q[:200] if q else None, "body": bool(body),
        }
        page = await store.query(filters, limit=max(1, min(limit, 200)),
                                 offset=max(0, offset), bucket_ms=BUCKETS.get(window, 3_600_000))
        page.update({"limit": max(1, min(limit, 200)), "offset": max(0, offset), "window": window})
        return page

    @router.get("/api/logs/{log_id}")
    async def log_detail(log_id: int) -> Any:
        if store is None:
            return _err(503, "请求日志不可用")
        found = await store.detail(log_id)
        if found is None:
            return _err(404, "日志不存在")
        return {"log": found}

    @router.delete("/api/logs")
    async def clear_logs() -> Any:
        if store is None:
            return _err(503, "请求日志不可用")
        return {"deleted": await store.delete_all()}

    @router.get("/api/models")
    async def models(refresh: int = 0) -> Any:
        return await models_payload(catalog, bool(refresh))

    @router.get("/api/accounts")
    async def accounts() -> Any:
        return await asyncio.to_thread(accounts_payload, chatgpt_auth, codebuddy_auth)

    @router.post("/api/accounts/refresh")
    async def refresh_accounts(request: Request) -> Any:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return _err(403, "跨站请求被拒绝")
        now = time.monotonic()
        if now - last_refresh["at"] < 5:
            return _err(429, "刷新过于频繁，请稍后再试")
        if refresh_lock.locked():
            return _err(409, "刷新正在进行")
        last_refresh["at"] = now
        results: dict[str, Any] = {}
        async with refresh_lock:
            pairs: tuple[tuple[str, Callable[[], Awaitable[Any]]], ...] = (
                ("openai", chatgpt_auth.authorize),
                ("codebuddy", codebuddy_auth.authorize),
            )
            outcomes = await asyncio.gather(*(call() for _, call in pairs), return_exceptions=True)
        for (provider, _), outcome in zip(pairs, outcomes):
            if isinstance(outcome, Exception):
                log.info("token refresh failed for %s: %s", provider, type(outcome).__name__)
                results[provider] = {"ok": False, "error": REFRESH_ERRORS[provider]}
            else:
                results[provider] = {"ok": True, "error": None}
        payload = await asyncio.to_thread(accounts_payload, chatgpt_auth, codebuddy_auth)
        payload["results"] = results
        return payload

    app.include_router(router)
    if STATIC_DIR.is_dir():
        app.mount("/dashboard/assets", StaticFiles(directory=STATIC_DIR), name="dashboard-assets")


def _enabled() -> bool:
    import os
    return os.getenv("DASHBOARD_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
