"""Manage AI-Proxy ChatGPT credentials and CodeBuddy desktop credentials."""

import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class UpstreamUnavailable(Exception):
    pass


def default_auth_file() -> Path:
    return Path.home() / ".config/ai-proxy/auth.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("credential file must contain an object")
        return data
    except (OSError, ValueError) as exc:
        raise UpstreamUnavailable(f"Cannot read valid credentials from {path}") from exc


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _jwt_identity(token: str) -> str | None:
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        auth = claims.get("https://api.openai.com/auth") or {}
        return json.dumps([claims.get("iss"), claims.get("sub"), auth.get("chatgpt_account_id"), claims.get("client_id"), claims.get("aud")])
    except (IndexError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Session:
    identity: str
    token: str
    headers: dict[str, str]
    base_url: str
    subscription: bool = False


class ChatGPTAuth:
    TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
    RESOURCE = "https://api.openai.com/v1"

    def __init__(self, path: Path, client: httpx.AsyncClient):
        self.path = path
        self.client = client
        self.lock = asyncio.Lock()

    async def authorize(self) -> Session:
        async with self.lock:
            try:
                data = _read_json(self.path)
            except UpstreamUnavailable as exc:
                raise UpstreamUnavailable("ChatGPT is not authorized; run python -m ai_proxy login") from exc
            cred = data.get("openai")
            if not isinstance(cred, dict):
                raise UpstreamUnavailable("ChatGPT is not authorized; run python -m ai_proxy login")
            kind = cred.get("type")
            if kind == "oauth":
                if not all(isinstance(cred.get(x), str) and cred[x] for x in ("access", "refresh", "clientId")):
                    raise UpstreamUnavailable("ChatGPT OAuth credential is incomplete; run python -m ai_proxy login")
                if not isinstance(cred.get("expires"), (int, float)) or cred["expires"] <= time.time() * 1000 + 60_000:
                    original = self.path.read_text()
                    try:
                        response = await self.client.post(self.TOKEN_URL, data={
                            "grant_type": "refresh_token", "client_id": cred["clientId"],
                            "refresh_token": cred["refresh"], "resource": self.RESOURCE,
                        }, timeout=15)
                        response.raise_for_status()
                        refreshed = response.json()
                        scopes = refreshed["scope"].split()
                        if "chatgpt.tokens.use.direct" not in scopes:
                            raise ValueError("Missing direct-token scope")
                        if not all(isinstance(refreshed.get(x), str) and refreshed[x] for x in ("access_token", "refresh_token")):
                            raise ValueError("Missing refreshed tokens")
                        expires_in = refreshed["expires_in"]
                        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
                            raise ValueError("Invalid token expiry")
                    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                        raise UpstreamUnavailable("ChatGPT token refresh failed; run python -m ai_proxy login") from exc
                    if self.path.read_text() != original:
                        return await self._authorize_after_change()
                    cred = {**cred, "access": refreshed["access_token"], "refresh": refreshed["refresh_token"],
                            "expires": time.time() * 1000 + expires_in * 1000 - 180_000, "scopes": scopes}
                    _atomic_json(self.path, {**data, "openai": cred})
                identity = _jwt_identity(cred["access"]) or hashlib.sha256(cred["refresh"].encode()).hexdigest()
                token = cred["access"]
                subscription = True
            elif kind == "api_key" and isinstance(cred.get("key"), str):
                token = cred["key"]
                identity = hashlib.sha256(token.encode()).hexdigest()
                subscription = False
            else:
                raise UpstreamUnavailable("Unsupported ChatGPT credential type")
            base = cred.get("baseUrl") if isinstance(cred.get("baseUrl"), str) else "https://api.openai.com/v1"
            return Session(hashlib.sha256(identity.encode()).hexdigest(), token, {}, base.rstrip("/"), subscription)

    async def _authorize_after_change(self) -> Session:
        # Another process refreshed the same file. Re-read once without taking
        # the non-reentrant lock again.
        data = _read_json(self.path).get("openai", {})
        if not isinstance(data, dict) or not isinstance(data.get("access"), str):
            raise UpstreamUnavailable("ChatGPT credential changed during refresh; retry")
        identity = _jwt_identity(data["access"]) or hashlib.sha256(str(data.get("refresh", "")).encode()).hexdigest()
        return Session(hashlib.sha256(identity.encode()).hexdigest(), data["access"], {},
                       str(data.get("baseUrl") or "https://api.openai.com/v1").rstrip("/"), True)


class CodeBuddyAuth:
    BASE = "https://copilot.tencent.com/v2"

    def __init__(self, client: httpx.AsyncClient, file: Path | None = None, directory: Path | None = None):
        self.client = client
        self.file = file
        self.directory = directory or self.default_directory()
        self.lock = asyncio.Lock()

    @staticmethod
    def default_directory() -> Path:
        home = Path.home()
        if sys.platform == "darwin":
            root = home / "Library/Application Support"
        elif sys.platform == "win32":
            root = Path(os.getenv("LOCALAPPDATA", home / "AppData/Local"))
        else:
            root = Path(os.getenv("XDG_DATA_HOME", home / ".local/share"))
        return root / "CodeBuddyExtension/Data/Public/auth"

    def _select(self) -> Path:
        if self.file:
            return self.file
        try:
            candidates = sorted(self.directory.glob("*.info"), key=lambda p: (-p.stat().st_mtime_ns, str(p)))
        except OSError as exc:
            raise UpstreamUnavailable("Cannot discover CodeBuddy credentials") from exc
        for path in candidates:
            try:
                self._validated(path)
                return path
            except UpstreamUnavailable:
                pass
        raise UpstreamUnavailable("CodeBuddy is not authorized; sign in to the desktop app")

    @staticmethod
    def _validated(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        data = _read_json(path)
        auth, account = data.get("auth"), data.get("account")
        if not isinstance(auth, dict) or not isinstance(account, dict) or not isinstance(auth.get("accessToken"), str) or not auth["accessToken"] or not isinstance(account.get("uid"), (str, int)):
            raise UpstreamUnavailable("Invalid CodeBuddy credential structure")
        for value in (auth.get("accessToken"), auth.get("refreshToken"), auth.get("domain"), account.get("uid"), account.get("enterpriseId")):
            if isinstance(value, str) and ("\r" in value or "\n" in value):
                raise UpstreamUnavailable("Invalid CodeBuddy credential header")
        return data, auth, account

    @staticmethod
    def _headers(auth: dict[str, Any], account: dict[str, Any]) -> dict[str, str]:
        enterprise = str(account.get("enterpriseId") or "")
        return {"X-User-Id": str(account["uid"]), "X-Enterprise-Id": enterprise,
                "X-Tenant-Id": enterprise, "X-Domain": str(auth.get("domain") or "www.codebuddy.cn"),
                "User-Agent": "ai-proxy/codebuddy"}

    async def authorize(self) -> Session:
        async with self.lock:
            path = self._select()
            data, auth, account = self._validated(path)
            if not isinstance(auth.get("expiresAt"), (int, float)) or auth["expiresAt"] <= time.time() * 1000 + 60_000:
                if not isinstance(auth.get("refreshToken"), str) or not auth["refreshToken"]:
                    raise UpstreamUnavailable("CodeBuddy token expired; sign in again")
                original = path.read_text()
                headers = self._headers(auth, account)
                try:
                    response = await self.client.post(self.BASE + "/plugin/auth/token/refresh", json={},
                        headers={**headers, "Authorization": "Bearer " + auth["accessToken"],
                                 "X-Refresh-Token": auth["refreshToken"], "X-Auth-Refresh-Source": "plugin"}, timeout=15)
                    response.raise_for_status()
                    body = response.json()
                    fresh = body["data"]
                    if body.get("code") != 0 or not isinstance(fresh, dict) or not isinstance(fresh.get("accessToken"), str) or not fresh["accessToken"]:
                        raise ValueError("Invalid refresh response")
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    raise UpstreamUnavailable("CodeBuddy token refresh failed; sign in again") from exc
                if path.read_text() != original:
                    data, auth, account = self._validated(path)
                else:
                    now = time.time() * 1000
                    auth = {**auth, **fresh, "refreshToken": fresh.get("refreshToken") or auth["refreshToken"],
                            "domain": fresh.get("domain") or auth.get("domain") or "www.codebuddy.cn",
                            "lastRefreshTime": now}
                    auth["expiresAt"] = fresh.get("expiresAt") or now + float(fresh.get("expiresIn", 0)) * 1000
                    if auth["expiresAt"] <= now + 60_000:
                        raise UpstreamUnavailable("CodeBuddy refresh returned invalid expiry")
                    _atomic_json(path, {**data, "auth": auth})
            headers = self._headers(auth, account)
            identity = hashlib.sha256(json.dumps(["codebuddy", headers["X-User-Id"], headers["X-Enterprise-Id"], headers["X-Domain"]]).encode()).hexdigest()
            return Session(identity, auth["accessToken"], headers, self.BASE)
