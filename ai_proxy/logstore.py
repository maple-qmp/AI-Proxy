"""SQLite-backed request log storage for the local dashboard.

Writes happen on a dedicated asyncio task so a slow disk can never block a
generation request; ``submit`` only enqueues. Reads use short-lived read-only
connections on worker threads.
"""

import asyncio
import contextlib
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger("ai_proxy.logstore")

TRUNCATION_MARKER = "\n…[truncated {omitted} bytes]"
_STOP = object()
# Upper bound on zero-filled histogram buckets, so an "all time" window cannot
# ask for billions of entries.
_MAX_BUCKETS = 500

_COLUMNS = (
    "ts", "route", "requested_model", "model_id", "provider", "status", "completed",
    "stream", "duration_ms", "ttfb_ms", "prompt_tokens", "completion_tokens",
    "total_tokens", "client", "client_detail", "upstream_request_id", "upstream_code",
    "error_category", "error_text", "request_body", "response_body", "request_bytes",
    "response_bytes", "request_truncated", "response_truncated",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  route TEXT NOT NULL,
  requested_model TEXT,
  model_id TEXT,
  provider TEXT,
  status INTEGER NOT NULL,
  completed INTEGER NOT NULL DEFAULT 1,
  stream INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  ttfb_ms INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  total_tokens INTEGER,
  client TEXT,
  client_detail TEXT,
  upstream_request_id TEXT,
  upstream_code INTEGER,
  error_category TEXT,
  error_text TEXT,
  request_body TEXT,
  response_body TEXT,
  request_bytes INTEGER NOT NULL DEFAULT 0,
  response_bytes INTEGER NOT NULL DEFAULT 0,
  request_truncated INTEGER NOT NULL DEFAULT 0,
  response_truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_ok_ts ON request_logs(status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(requested_model, ts DESC);
"""

_INSERT = (
    "INSERT INTO request_logs (" + ", ".join(_COLUMNS) + ") VALUES ("
    + ", ".join("?" * len(_COLUMNS)) + ")"
)

# List rows never carry the bodies themselves: they only report their size and a
# short preview, so a page of 200 rows stays small even with 256 KiB bodies.
_LIST_COLS = """
  id, ts, route, requested_model, model_id, provider, status, completed, stream,
  duration_ms, ttfb_ms, prompt_tokens, completion_tokens, total_tokens, client,
  client_detail, upstream_request_id, upstream_code, error_category, error_text,
  request_bytes, response_bytes, request_truncated, response_truncated,
  length(request_body) AS request_body_len,
  substr(request_body, 1, 160) AS request_preview
"""


def encode_body(raw: bytes | str | None, limit: int) -> tuple[str | None, int, bool]:
    """Return ``(text, original_size, truncated)`` and never raise.

    Decoding with ``replace`` keeps a logged request from ever failing on odd
    bytes, and truncation happens before enqueueing so the queue holds at most
    ``limit`` bytes per body.
    """
    if raw is None:
        return None, 0, False
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace")
    size = len(data)
    if size <= limit:
        return data.decode("utf-8", "replace"), size, False
    marker = TRUNCATION_MARKER.format(omitted=size)
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= limit:
        # A limit smaller than the marker itself: hard-cut so the cap still holds.
        return data[:limit].decode("utf-8", "replace"), size, True
    keep = limit - len(marker_bytes)
    text = data[:keep].decode("utf-8", "replace") + TRUNCATION_MARKER.format(omitted=size - keep)
    return text, size, True


def client_label(user_agent: str | None) -> str:
    """Map a User-Agent to a short, stable client name."""
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    if "claude-cli" in ua or "claude-code" in ua:
        return "claude-code"
    if "openai-python" in ua or "openai/python" in ua:
        return "openai-python"
    if "curl" in ua:
        return "curl"
    if "httpx" in ua:
        return "httpx"
    if any(token in ua for token in ("node", "axios", "undici")):
        return "node"
    return "other"


def _display_path(path: Path) -> str:
    try:
        return str(path).replace(str(Path.home()), "~", 1) if path.is_relative_to(Path.home()) else str(path)
    except (OSError, ValueError):
        return str(path)


@dataclass
class LogRecord:
    ts: int
    route: str
    requested_model: str | None = None
    model_id: str | None = None
    provider: str | None = None
    status: int = 500
    completed: int = 1
    stream: int = 0
    duration_ms: int = 0
    ttfb_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    client: str | None = None
    client_detail: str | None = None
    upstream_request_id: str | None = None
    upstream_code: int | None = None
    error_category: str | None = None
    error_text: str | None = None
    request_body: str | None = None
    response_body: str | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    request_truncated: int = 0
    response_truncated: int = 0

    def as_row(self) -> tuple[Any, ...]:
        # Bodies are text columns; normalising here keeps a stray bytes value
        # from being stored as a BLOB that later reads back as bytes.
        values = []
        for column in _COLUMNS:
            value = getattr(self, column)
            if column in ("request_body", "response_body") and isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            values.append(value)
        return tuple(values)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses = ["ts >= ?", "ts < ?"]
    args: list[Any] = [filters["since"], filters["until"]]
    status = filters.get("status")
    if status == "success":
        clauses.append("status < 400")
    elif status == "error":
        clauses.append("status >= 400")
    elif isinstance(status, str) and len(status) == 3 and status[0] in "2345":
        base = int(status[0]) * 100
        clauses += ["status >= ?", "status < ?"]
        args += [base, base + 100]
    for column in ("provider", "route", "client"):
        value = filters.get(column)
        if value:
            clauses.append(f"{column} = ?")
            args.append(value)
    model = filters.get("model")
    if model:
        clauses.append("(model_id = ? OR requested_model = ?)")
        args += [model, model]
    needle = filters.get("q")
    if needle:
        pattern = "%" + _escape_like(needle) + "%"
        columns = ["requested_model", "model_id", "upstream_request_id", "error_text", "client_detail"]
        if filters.get("body"):
            columns += ["request_body", "response_body"]
        clauses.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns) + ")")
        args += [pattern] * len(columns)
    return " AND ".join(clauses), args


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "route": row["route"],
        "requested_model": row["requested_model"],
        "model_id": row["model_id"],
        "provider": row["provider"],
        "status": row["status"],
        "completed": bool(row["completed"]),
        "stream": bool(row["stream"]),
        "duration_ms": row["duration_ms"],
        "ttfb_ms": row["ttfb_ms"],
        "tokens": {
            "prompt": row["prompt_tokens"],
            "completion": row["completion_tokens"],
            "total": row["total_tokens"],
        },
        "client": row["client"],
        "client_detail": row["client_detail"],
        "upstream_request_id": row["upstream_request_id"],
        "upstream_code": row["upstream_code"],
        "error": {"category": row["error_category"], "text": row["error_text"]},
        "bodies": {
            "request_bytes": row["request_bytes"],
            "response_bytes": row["response_bytes"],
            "request_truncated": bool(row["request_truncated"]),
            "response_truncated": bool(row["response_truncated"]),
            "request_body_len": row["request_body_len"],
        },
        "request_preview": row["request_preview"],
    }


def _empty_facets() -> dict[str, list[str]]:
    return {"providers": [], "routes": [], "clients": [], "models": []}


def _empty_summary() -> dict[str, Any]:
    return {"total": 0, "success": 0, "errors": 0, "error_rate": 0.0, "streams": 0,
            "incomplete": 0, "latency_ms": {"avg": 0, "p50": 0, "p95": 0},
            "tokens": {"prompt": 0, "completion": 0, "total": 0}}


def _percentiles(conn: sqlite3.Connection, since: int, until: int) -> tuple[int, int]:
    count = conn.execute(
        "SELECT COUNT(*) FROM request_logs WHERE ts >= ? AND ts < ?", (since, until)
    ).fetchone()[0]
    if not count:
        return 0, 0

    def at(offset: int) -> int:
        row = conn.execute(
            "SELECT duration_ms FROM request_logs WHERE ts >= ? AND ts < ?"
            " ORDER BY duration_ms, id LIMIT 1 OFFSET ?",
            (since, until, offset),
        ).fetchone()
        return int(row[0]) if row else 0

    return at((count - 1) // 2), at(math.ceil(0.95 * count) - 1)


def _summary(conn: sqlite3.Connection, since: int, until: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN status < 400 THEN 1 ELSE 0 END), 0) AS success,"
        " COALESCE(SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END), 0) AS errors,"
        " COALESCE(SUM(stream), 0) AS streams,"
        " COALESCE(SUM(CASE WHEN completed = 0 THEN 1 ELSE 0 END), 0) AS incomplete,"
        " COALESCE(AVG(duration_ms), 0) AS avg_ms,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens), 0) AS total_tokens"
        " FROM request_logs WHERE ts >= ? AND ts < ?",
        (since, until),
    ).fetchone()
    total = int(row["total"])
    p50, p95 = _percentiles(conn, since, until)
    return {
        "total": total,
        "success": int(row["success"]),
        "errors": int(row["errors"]),
        "error_rate": round(int(row["errors"]) / total, 4) if total else 0.0,
        "streams": int(row["streams"]),
        "incomplete": int(row["incomplete"]),
        "latency_ms": {"avg": int(row["avg_ms"]), "p50": p50, "p95": p95},
        "tokens": {"prompt": int(row["prompt_tokens"]),
                   "completion": int(row["completion_tokens"]),
                   "total": int(row["total_tokens"])},
    }


def _histogram(conn: sqlite3.Connection, since: int, until: int, width: int) -> list[dict[str, Any]]:
    if width <= 0:
        return []
    # Zero-filled output is one dict per bucket, so an unbounded window (or a
    # caller-supplied width) would allocate millions of empties. Widen the
    # bucket instead of capping the response length.
    if until - since > width * _MAX_BUCKETS:
        width = max(width, (until - since + _MAX_BUCKETS - 1) // _MAX_BUCKETS)
    start = since - (since % width)
    rows = conn.execute(
        "SELECT (ts - ?) / ? AS bucket, COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END), 0) AS errors"
        " FROM request_logs WHERE ts >= ? AND ts < ? GROUP BY bucket ORDER BY bucket",
        (start, width, since, until),
    ).fetchall()
    found = {int(row["bucket"]): (int(row["total"]), int(row["errors"])) for row in rows}
    count = (until - start + width - 1) // width
    return [
        {"ts": start + index * width,
         "total": found.get(index, (0, 0))[0],
         "errors": found.get(index, (0, 0))[1]}
        for index in range(count)
    ]


def _by_provider(conn: sqlite3.Connection, since: int, until: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT COALESCE(provider, 'unknown') AS provider, route, COUNT(*) AS count,"
        " COALESCE(SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END), 0) AS errors"
        " FROM request_logs WHERE ts >= ? AND ts < ? GROUP BY provider, route ORDER BY count DESC",
        (since, until),
    ).fetchall()
    return [{"provider": row["provider"], "route": row["route"],
             "count": int(row["count"]), "errors": int(row["errors"])} for row in rows]


def _top_models(conn: sqlite3.Connection, since: int, until: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(model_id, ''), requested_model, '(unknown)') AS name,"
        " COALESCE(provider, 'unknown') AS provider, COUNT(*) AS count,"
        " COALESCE(SUM(total_tokens), 0) AS tokens, COALESCE(AVG(duration_ms), 0) AS avg_ms"
        " FROM request_logs WHERE ts >= ? AND ts < ? GROUP BY name, provider"
        " ORDER BY count DESC LIMIT 10",
        (since, until),
    ).fetchall()
    return [{"name": row["name"], "provider": row["provider"], "count": int(row["count"]),
             "tokens": int(row["tokens"]), "avg_ms": int(row["avg_ms"])} for row in rows]


def _facets(conn: sqlite3.Connection, since: int, until: int) -> dict[str, list[str]]:
    queries = {
        "providers": "SELECT DISTINCT provider FROM request_logs WHERE ts >= ? AND ts < ?"
                     " AND provider IS NOT NULL ORDER BY provider LIMIT 50",
        "routes": "SELECT DISTINCT route FROM request_logs WHERE ts >= ? AND ts < ?"
                  " AND route IS NOT NULL ORDER BY route LIMIT 50",
        "clients": "SELECT DISTINCT client FROM request_logs WHERE ts >= ? AND ts < ?"
                   " AND client IS NOT NULL ORDER BY client LIMIT 50",
        "models": "SELECT DISTINCT COALESCE(NULLIF(model_id, ''), requested_model) AS name"
                  " FROM request_logs WHERE ts >= ? AND ts < ?"
                  " AND COALESCE(model_id, requested_model) IS NOT NULL ORDER BY name LIMIT 50",
    }
    facets: dict[str, list[str]] = {}
    for name, sql in queries.items():
        try:
            facets[name] = [row[0] for row in conn.execute(sql, (since, until)).fetchall() if row[0]]
        except sqlite3.Error:
            facets[name] = []
    return facets


class LogStore:
    def __init__(self, path: Path | str, *, max_rows: int = 5000, body_limit: int = 262144,
                 queue_size: int = 256, batch_size: int = 100) -> None:
        self.path = Path(path).expanduser()
        self.max_rows = max_rows
        self.body_limit = body_limit
        self.batch_size = batch_size
        self.queue_size = queue_size
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size)
        self._writer: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._initialized = False
        self._init_error: str | None = None
        self._dropped = 0
        self._failed_batches = 0

    # ------------------------------------------------------------------ write

    async def start(self) -> None:
        """Open the database eagerly; failures disable logging, never the app."""
        if self._initialized or self._init_error is not None:
            self._ensure_writer()
            return
        try:
            await asyncio.to_thread(self._init_db)
        except Exception as exc:  # noqa: BLE001 - a log store must never break startup
            self._init_error = "initialization failed"
            log.error("request logging disabled: %s", type(exc).__name__)
            return
        self._initialized = True
        self._ensure_writer()

    def submit(self, record: LogRecord) -> None:
        """Enqueue a finished record. Synchronous on purpose: this runs from a
        streaming generator's ``finally``, where awaiting can be cancelled."""
        if self._stopping or not self._ensure_writer():
            self._dropped += 1
            return
        self._cap(record)
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped += 1

    def _cap(self, record: LogRecord) -> None:
        """Apply the body limit at the boundary, so the queue never holds more
        than ``body_limit`` per body regardless of who built the record."""
        for field, size_field, flag_field in (
            ("request_body", "request_bytes", "request_truncated"),
            ("response_body", "response_bytes", "response_truncated"),
        ):
            value = getattr(record, field)
            if value is None:
                continue
            text, size, truncated = encode_body(value, self.body_limit)
            setattr(record, field, text)
            if not getattr(record, size_field):
                setattr(record, size_field, size)
            if truncated:
                setattr(record, flag_field, 1)

    def _ensure_writer(self) -> bool:
        if self._init_error is not None:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._writer is not None and not self._writer.done() and self._loop is loop:
            return True
        if self._writer is not None and not self._writer.done():
            # A different loop owns the current writer; let it go.
            with contextlib.suppress(Exception):
                self._writer.cancel()
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._stopping = False
        self._writer = loop.create_task(self._run(), name="ai-proxy-log-writer")
        return True

    async def _run(self) -> None:
        if not self._initialized:
            try:
                await asyncio.to_thread(self._init_db)
                self._initialized = True
            except Exception as exc:  # noqa: BLE001
                self._init_error = "initialization failed"
                log.error("request logging disabled: %s", type(exc).__name__)
                self._drain()
                return
        while True:
            record = await self._queue.get()
            consumed = 1
            batch: list[LogRecord] = []
            try:
                if record is _STOP:
                    break
                batch.append(record)
                while len(batch) < self.batch_size:
                    try:
                        following = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    consumed += 1
                    if following is _STOP:
                        self._stopping = True
                        break
                    batch.append(following)
                if batch:
                    try:
                        await asyncio.to_thread(self._write_batch, batch)
                    except Exception as exc:  # noqa: BLE001
                        self._failed_batches += 1
                        log.error("log batch dropped: %s", type(exc).__name__)
            finally:
                for _ in range(consumed):
                    self._queue.task_done()
            if self._stopping:
                break
        self._drain()

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queue.task_done()

    async def flush(self) -> None:
        writer = self._writer
        if writer is None or writer.done():
            return
        await self._queue.join()

    async def stop(self, timeout: float = 5.0) -> None:
        writer = self._writer
        if writer is None:
            return
        if not writer.done():
            self._stopping = True
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_STOP)
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(writer), timeout)
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer
        self._writer = None
        self._loop = None

    async def prune(self) -> None:
        if not self.path.exists():
            return
        await asyncio.to_thread(self._prune_once)

    def _prune_once(self) -> None:
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            self._prune(conn)
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(_SCHEMA)
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            elif version != 1:
                raise RuntimeError(f"unsupported log schema version {version}")
            self._prune(conn)
            conn.commit()
        finally:
            conn.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.chmod(Path(str(self.path) + suffix), 0o600)

    def _prune(self, conn: sqlite3.Connection) -> None:
        if self.max_rows <= 0:
            return
        conn.execute(
            "DELETE FROM request_logs WHERE id <= ("
            "SELECT id FROM request_logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (self.max_rows,),
        )

    def _write_batch(self, batch: list[LogRecord]) -> None:
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executemany(_INSERT, [record.as_row() for record in batch])
            # Pruning on every batch keeps the table bounded even under a burst;
            # the DELETE is a single primary-key walk with nothing to remove in
            # the common case.
            self._prune(conn)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------- read

    def _ro(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        return conn

    async def query(self, filters: dict[str, Any], *, limit: int, offset: int,
                    bucket_ms: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._query_sync, filters, limit, offset, bucket_ms)

    def _query_sync(self, filters: dict[str, Any], limit: int, offset: int,
                    bucket_ms: int) -> dict[str, Any]:
        conn = self._ro()
        since, until = filters["since"], filters["until"]
        if conn is None:
            return {"items": [], "total": 0, "summary": _empty_summary(), "histogram": [],
                    "by_provider": [], "top_models": [], "facets": _empty_facets()}
        try:
            where, args = _where(filters)
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM request_logs WHERE {where}", args).fetchone()[0])
            rows = conn.execute(
                f"SELECT {_LIST_COLS} FROM request_logs WHERE {where}"
                " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            return {
                "items": [_row_dict(row) for row in rows],
                "total": total,
                "summary": _summary(conn, since, until),
                "histogram": _histogram(conn, since, until, bucket_ms),
                "by_provider": _by_provider(conn, since, until),
                "top_models": _top_models(conn, since, until),
                "facets": _facets(conn, since, until),
            }
        finally:
            conn.close()

    async def detail(self, log_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._detail_sync, log_id)

    def _detail_sync(self, log_id: int) -> dict[str, Any] | None:
        conn = self._ro()
        if conn is None:
            return None
        try:
            row = conn.execute(
                f"SELECT {_LIST_COLS}, request_body, response_body"
                " FROM request_logs WHERE id = ?", (log_id,)).fetchone()
            if row is None:
                return None
            data = _row_dict(row)
            data["bodies"]["request"] = row["request_body"]
            data["bodies"]["response"] = row["response_body"]
            return data
        finally:
            conn.close()

    async def earliest(self) -> int | None:
        return await asyncio.to_thread(self._earliest_sync)

    def _earliest_sync(self) -> int | None:
        conn = self._ro()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT MIN(ts) FROM request_logs").fetchone()
            return int(row[0]) if row and row[0] is not None else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    async def delete_all(self) -> int:
        return await asyncio.to_thread(self._delete_all_sync)

    def _delete_all_sync(self) -> int:
        if not self.path.exists():
            return 0
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            count = int(conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0])
            conn.execute("DELETE FROM request_logs")
            conn.commit()
            conn.execute("VACUUM")
            return count
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    async def info(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._info_sync)

    def _info_sync(self) -> dict[str, Any]:
        rows = 0
        conn = self._ro()
        if conn is not None:
            try:
                rows = int(conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0])
            except sqlite3.Error:
                rows = 0
            finally:
                conn.close()
        size = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                size += Path(str(self.path) + suffix).stat().st_size
        return {
            "enabled": self._init_error is None,
            "path": _display_path(self.path),
            "size_bytes": size,
            "rows": rows,
            "max_rows": self.max_rows,
            "body_limit": self.body_limit,
            "dropped": self._dropped,
            "failed_batches": self._failed_batches,
            "error": self._init_error,
        }
