"""Interactive Sign in with ChatGPT for this installation of AI-Proxy."""

import asyncio
import base64
import hashlib
import os
import secrets
import sys
import tempfile
import uuid
import webbrowser
from html import escape
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .auth import _atomic_json, _read_json, default_auth_file


AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
CALLBACK_PORT = 1455


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _device_id(path: Path) -> str:
    try:
        value = path.read_text().strip()
        parsed = str(uuid.UUID(value))
        os.chmod(path, 0o600)
        return parsed
    except FileNotFoundError:
        pass
    except ValueError as exc:
        raise ValueError(f"Invalid AI-Proxy device ID in {path}") from exc
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = str(uuid.uuid4())
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value + "\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return value


def _authorization_result(url: str, redirect_uri: str, state: str) -> tuple[str, str]:
    actual, expected = urlsplit(url.strip()), urlsplit(redirect_uri)
    if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path):
        raise ValueError("Callback URL does not match the AI-Proxy redirect URI")
    query = parse_qs(actual.query)
    if query.get("state") != [state]:
        raise ValueError("OAuth state mismatch")
    if query.get("error"):
        raise ValueError("ChatGPT authorization failed: " + query["error"][0])
    code, client_id = query.get("code", []), query.get("client_id", [])
    if len(code) != 1 or not code[0] or len(client_id) != 1 or not client_id[0].strip():
        raise ValueError("Callback is missing authorization code or issued client ID")
    return code[0], client_id[0].strip()


async def _manual_url() -> str | None:
    """Read one pasted callback URL, or None once standard input is closed.

    A blank line returns "" so the caller can prompt again; EOF must be
    distinguishable from it, otherwise the login waits on a dead terminal.
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str | None] = loop.create_future()

    def read() -> None:
        try:
            value = sys.stdin.readline()
        except (OSError, RuntimeError):
            value = ""
        # readline() returns "" only at EOF; a bare ENTER yields "\n".
        entered: str | None = None if value == "" else value.strip()
        loop.call_soon_threadsafe(lambda: not result.done() and result.set_result(entered))

    import threading
    threading.Thread(target=read, daemon=True).start()
    return await result


async def login_chatgpt(
    path: Path | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    port: int = CALLBACK_PORT,
    open_browser: Callable[[str], object] = webbrowser.open,
    manual_url: Callable[[], Awaitable[str]] = _manual_url,
) -> Path:
    """Complete OAuth and save credentials only after validating the token grant."""
    path = path or default_auth_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    device = _device_id(path.parent / ".ai-proxy-device-id")
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state, nonce = _b64url(secrets.token_bytes(32)), _b64url(secrets.token_bytes(32))
    loop = asyncio.get_running_loop()
    callback: asyncio.Future[tuple[str, str]] = loop.create_future()
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, message = 400, "Invalid OAuth callback."
        result: tuple[str, str] | None = None
        failure: ValueError | None = None
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            method, target, _version = request.decode("ascii").strip().split(" ", 2)
            if method != "GET":
                status, message = 405, "Method not allowed."
            elif urlsplit(target).path != CALLBACK_PATH:
                status, message = 404, "Callback route not found."
            elif callback.done():
                status, message = 409, "This sign-in has already been handled."
            else:
                result = _authorization_result(redirect_uri.replace(CALLBACK_PATH, target, 1), redirect_uri, state)
                status, message = 200, "ChatGPT authentication completed. You can close this window."
        except (ValueError, UnicodeError, asyncio.TimeoutError, asyncio.LimitOverrunError) as exc:
            message = str(exc) or message
            if message.startswith("ChatGPT authorization failed:") and not callback.done():
                failure = ValueError(message)
        finally:
            body = f"<!doctype html><meta charset='utf-8'><title>AI-Proxy login</title><p>{escape(message)}</p>".encode()
            try:
                writer.write(f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
            except (ConnectionError, OSError):
                # The browser already went away; the parsed result is still valid.
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            if result is not None and not callback.done():
                callback.set_result(result)
            elif failure is not None and not callback.done():
                callback.set_exception(failure)

    try:
        server = await asyncio.start_server(handle, "127.0.0.1", port, limit=8192)
    except OSError as exc:
        raise RuntimeError(f"Cannot listen on OAuth callback port {port}; close the other login and retry") from exc
    try:
        actual_port = server.sockets[0].getsockname()[1]
        redirect_uri = f"http://127.0.0.1:{actual_port}{CALLBACK_PATH}"
        query = urlencode({
            "client_id": "dynamic_agent_client", "agent_name_hint": "AI-Proxy",
            "ext_agent_host_id": f"urn:uuid:{device}", "response_type": "code",
            "redirect_uri": redirect_uri, "resource": RESOURCE, "scope": SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "nonce": nonce,
        })
        url = AUTHORIZE_URL + "?" + query
        print(f"Open this URL to sign in to ChatGPT:\n{url}\n")
        open_browser(url)
        print("Waiting for the browser callback. If it cannot reach this computer, paste the full callback URL here:")
        manual: asyncio.Task[str | None] | None = asyncio.create_task(manual_url())
        try:
            while True:
                assert manual is not None
                done, _ = await asyncio.wait((callback, manual), return_when=asyncio.FIRST_COMPLETED)
                if callback in done:
                    # Raises the callback's own error, e.g. state mismatch or denial.
                    code, client_id = await callback
                    break
                entered = await manual
                if entered is None:
                    raise ValueError("Standard input closed before the sign-in completed")
                if not entered:
                    # A stray ENTER must not cancel the pending browser callback.
                    manual = asyncio.create_task(manual_url())
                    continue
                try:
                    code, client_id = _authorization_result(entered, redirect_uri, state)
                except ValueError as exc:
                    print(f"{exc}. Paste the full callback URL again, or finish in the browser:")
                    manual = asyncio.create_task(manual_url())
                    continue
                break
        finally:
            if manual is not None:
                manual.cancel()
            if not callback.done():
                callback.cancel()
        owned_client = client is None
        client = client or httpx.AsyncClient()
        try:
            response = await client.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "client_id": client_id, "code": code,
                "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE,
            }, timeout=15)
            response.raise_for_status()
            token = response.json()
        finally:
            if owned_client:
                await client.aclose()
        if not isinstance(token, dict):
            raise ValueError("Invalid ChatGPT token response")
        for field in ("access_token", "refresh_token", "id_token", "scope"):
            if not isinstance(token.get(field), str) or not token[field].strip():
                raise ValueError(f"ChatGPT token response is missing {field}")
        scopes = token["scope"].split()
        if "chatgpt.tokens.use.direct" not in scopes:
            raise ValueError("ChatGPT grant lacks direct-token scope")
        expires = token.get("expires_in")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 180:
            raise ValueError("Invalid ChatGPT token expiry")
        import time
        try:
            data = _read_json(path)
        except Exception as exc:
            if path.exists():
                raise ValueError(f"Existing credential file is invalid: {path}") from exc
            data = {}
        data["openai"] = {"type": "oauth", "access": token["access_token"],
                          "refresh": token["refresh_token"], "expires": time.time() * 1000 + expires * 1000 - 180_000,
                          "clientId": client_id, "scopes": scopes}
        _atomic_json(path, data)
        print(f"ChatGPT credentials saved to {path}")
        return path
    finally:
        server.close()
        await server.wait_closed()
