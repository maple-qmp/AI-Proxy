import os
import sys
from pathlib import Path

import uvicorn


def load_service_env() -> None:
    path = Path(os.getenv("SERVICE_ENV_FILE", Path.home() / ".config/ai-proxy/service.env"))
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"HOST", "PORT", "GATEWAY_API_KEY", "GATEWAY_AUTH_FILE", "CODEBUDDY_AUTH_FILE",
                       "CODEBUDDY_PROMPT_COMPAT", "CODEBUDDY_DEBUG", "SAFEGUARDS_CLASSIFIER_MODEL",
                       "DASHBOARD_ENABLED", "LOG_ENABLED", "LOG_DB_PATH", "LOG_MAX_ROWS",
                       "LOG_BODY_LIMIT_BYTES", "LOG_QUEUE_SIZE"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main() -> None:
    load_service_env()
    if len(sys.argv) == 2 and sys.argv[1] == "login":
        import asyncio
        from .auth import default_auth_file
        from .login import login_chatgpt

        try:
            asyncio.run(login_chatgpt(Path(os.environ["GATEWAY_AUTH_FILE"]) if os.getenv("GATEWAY_AUTH_FILE") else default_auth_file()))
        except (KeyboardInterrupt, Exception) as exc:
            raise SystemExit(f"ChatGPT login failed: {exc}") from exc
        return
    if len(sys.argv) != 1:
        raise SystemExit("Usage: python -m ai_proxy [login]")
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8787"))
    if host not in ("127.0.0.1", "::1", "localhost") and not os.getenv("GATEWAY_API_KEY"):
        raise SystemExit("GATEWAY_API_KEY is required for non-loopback binding")
    if not 1 <= port <= 65535:
        raise SystemExit("PORT must be between 1 and 65535")
    uvicorn.run("ai_proxy.app:app", host=host, port=port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
