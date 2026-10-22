import asyncio
import os
import sqlite3
import time

from ai_proxy.logstore import LogRecord, LogStore, client_label, encode_body

WIDE = {"since": 0, "until": 2**62}


def record(**overrides):
    values = {"ts": int(time.time() * 1000), "route": "chat/completions", "status": 200,
              "requested_model": "glm", "model_id": "glm", "provider": "codebuddy",
              "duration_ms": 12, "client": "curl"}
    values.update(overrides)
    return LogRecord(**values)


def run(coro):
    return asyncio.run(coro)


def page_of(store, **filters):
    return store.query({**WIDE, **filters}, limit=100, offset=0, bucket_ms=3_600_000)


def test_permissions_schema_and_wal(tmp_path):
    path = tmp_path / "logs.db"
    store = LogStore(path)

    async def main():
        await store.start()
        store.submit(record())
        await store.flush()
        await store.stop()

    run(main())
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path).st_mode & 0o777 == 0o700
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0] == 1
    finally:
        conn.close()


def test_submit_without_start_is_lazy(tmp_path):
    store = LogStore(tmp_path / "logs.db")

    async def main():
        store.submit(record())
        await store.flush()
        return await page_of(store)

    page = run(main())
    assert page["total"] == 1
    assert page["items"][0]["requested_model"] == "glm"


def test_bodies_truncated_with_marker(tmp_path):
    store = LogStore(tmp_path / "logs.db", body_limit=1024)

    async def main():
        store.submit(record(request_body="x" * 5000, response_body="short"))
        await store.flush()
        page = await page_of(store)
        return page, await store.detail(page["items"][0]["id"])

    page, detail = run(main())
    row = page["items"][0]
    assert row["bodies"]["request_truncated"] is True
    assert row["bodies"]["request_bytes"] == 5000
    assert detail["bodies"]["response"] == "short"
    assert "truncated" in detail["bodies"]["request"]
    assert len(detail["bodies"]["request"].encode()) <= 1024


def test_invalid_utf8_never_raises(tmp_path):
    store = LogStore(tmp_path / "logs.db")

    async def main():
        store.submit(record(request_body=b"\xff\xfe\x00payload"))
        await store.flush()
        page = await page_of(store)
        return await store.detail(page["items"][0]["id"])

    detail = run(main())
    assert "�" in detail["bodies"]["request"]


def test_retention_prunes_old_rows(tmp_path):
    store = LogStore(tmp_path / "logs.db", max_rows=10)

    async def main():
        base = 1_700_000_000_000
        for index in range(25):
            store.submit(record(ts=base + index))
        await store.flush()
        return await page_of(store)

    page = run(main())
    assert page["total"] == 10
    stamps = sorted(row["ts"] for row in page["items"])
    assert stamps[0] == 1_700_000_000_015
    assert stamps[-1] == 1_700_000_000_024


def test_filters_and_pagination(tmp_path):
    store = LogStore(tmp_path / "logs.db")
    base = 1_700_000_000_000
    window = {"since": base, "until": base + 100}

    async def main():
        for sample in (
            record(ts=base + 1, status=200, provider="openai", route="responses",
                   requested_model="gpt", model_id="gpt-1"),
            record(ts=base + 2, status=400, provider="codebuddy", route="messages",
                   requested_model="glm", model_id=None, error_text="bad request"),
            record(ts=base + 3, status=503, provider="codebuddy", route="messages",
                   requested_model="glm", model_id=None, error_text="upstream gone"),
            record(ts=base + 4, status=200, provider="codebuddy", route="chat/completions",
                   requested_model="glm", model_id=None, client="claude-code",
                   request_body='{"q":"needle"}'),
        ):
            store.submit(sample)
        await store.flush()

        async def filters(**extra):
            return await store.query({**window, **extra}, limit=10, offset=0, bucket_ms=1000)

        return {
            "all": await filters(),
            "error": await filters(status="error"),
            "five": await filters(status="5xx"),
            "provider": await filters(provider="openai"),
            "route": await filters(route="messages"),
            "client": await filters(client="claude-code"),
            "model": await filters(model="glm"),
            "keyword": await filters(q="upstream"),
            "body": await filters(q="needle", body=True),
            "body_off": await filters(q="needle", body=False),
            "wildcard": await filters(q="%"),
            "page2": await store.query(window, limit=1, offset=1, bucket_ms=1000),
        }

    pages = run(main())
    assert pages["all"]["total"] == 4
    assert pages["error"]["total"] == 2
    assert pages["five"]["total"] == 1
    assert pages["provider"]["total"] == 1
    assert pages["route"]["total"] == 2
    assert pages["client"]["total"] == 1
    assert pages["model"]["total"] == 3
    assert pages["keyword"]["total"] == 1
    assert pages["body"]["total"] == 1
    assert pages["body_off"]["total"] == 0
    assert pages["wildcard"]["total"] == 0
    assert len(pages["page2"]["items"]) == 1
    assert pages["page2"]["total"] == 4
    assert pages["all"]["facets"]["providers"] == ["codebuddy", "openai"]


def test_summary_percentiles_and_histogram(tmp_path):
    store = LogStore(tmp_path / "logs.db")
    base = 1_700_000_000_000

    async def main():
        for index, duration in enumerate([10, 20, 30, 40, 50]):
            store.submit(record(ts=base + index * 1000, duration_ms=duration,
                                prompt_tokens=index, completion_tokens=index * 2,
                                total_tokens=index * 3))
        store.submit(record(ts=base + 6000, status=500, duration_ms=99))
        # A seventh sample makes the count odd, so the median is unambiguous:
        # sorted durations are 10, 20, 30, 40, 50, 70, 99 and the middle is 40.
        store.submit(record(ts=base + 7000, duration_ms=70))
        await store.flush()
        return await store.query({"since": base, "until": base + 20_000}, limit=50,
                                 offset=0, bucket_ms=5000)

    page = run(main())
    summary = page["summary"]
    assert summary["total"] == 7
    assert summary["errors"] == 1
    assert summary["latency_ms"]["p50"] == 40
    assert summary["latency_ms"]["p95"] == 99
    assert summary["tokens"] == {"prompt": 10, "completion": 20, "total": 30}
    assert sum(bucket["total"] for bucket in page["histogram"]) == 7
    assert sum(bucket["errors"] for bucket in page["histogram"]) == 1
    assert len(page["histogram"]) == 4
    assert page["by_provider"][0]["count"] == 7


def test_empty_window_summary(tmp_path):
    store = LogStore(tmp_path / "logs.db")

    async def main():
        await store.start()
        return await page_of(store)

    page = run(main())
    assert page["items"] == []
    assert page["total"] == 0
    assert page["summary"]["latency_ms"] == {"avg": 0, "p50": 0, "p95": 0}


def test_all_time_window_does_not_explode_the_histogram(tmp_path):
    """Regression: the 'all time' window used to zero-fill one bucket per slot,
    which is ~2^62 entries for an unbounded range."""
    store = LogStore(tmp_path / "logs.db")

    async def main():
        store.submit(record(ts=1_700_000_000_000))
        await store.flush()
        return await store.query({"since": 0, "until": 2**62}, limit=10, offset=0,
                                 bucket_ms=3_600_000)

    page = run(main())
    assert 0 < len(page["histogram"]) <= 500
    assert sum(bucket["total"] for bucket in page["histogram"]) == 1
    # Buckets stay contiguous after widening, so the chart has no gaps.
    widths = {page["histogram"][i + 1]["ts"] - page["histogram"][i]["ts"]
              for i in range(len(page["histogram"]) - 1)}
    assert len(widths) <= 1


def test_stop_drains_pending(tmp_path):
    path = tmp_path / "logs.db"
    store = LogStore(path)

    async def main():
        await store.start()
        for _ in range(5):
            store.submit(record())
        await store.stop()

    run(main())
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0] == 5
    finally:
        conn.close()


def test_init_failure_disables_without_raising(tmp_path):
    (tmp_path / "logs.db").mkdir()
    store = LogStore(tmp_path / "logs.db")

    async def main():
        await store.start()
        store.submit(record())
        await store.flush()
        page = await page_of(store)
        info = await store.info()
        await store.stop()
        return page, info

    page, info = run(main())
    assert page["items"] == []
    assert info["enabled"] is False
    assert info["error"] == "initialization failed"


def test_detail_missing_returns_none(tmp_path):
    store = LogStore(tmp_path / "logs.db")
    assert run(store.detail(42)) is None


def test_delete_all(tmp_path):
    store = LogStore(tmp_path / "logs.db")

    async def main():
        store.submit(record())
        store.submit(record())
        await store.flush()
        deleted = await store.delete_all()
        return deleted, await page_of(store)

    deleted, page = run(main())
    assert deleted == 2
    assert page["total"] == 0


def test_encode_body_helpers():
    assert encode_body(None, 10) == (None, 0, False)
    assert encode_body(b"abc", 10) == ("abc", 3, False)
    text, size, truncated = encode_body(b"a" * 100, 20)
    assert size == 100 and truncated is True
    assert len(text.encode()) <= 20


def test_client_label():
    assert client_label("claude-cli/2.0.0 (external, cli)") == "claude-code"
    assert client_label("OpenAI/Python 1.40.0") == "openai-python"
    assert client_label("curl/8.7.1") == "curl"
    assert client_label("python-httpx/0.28.1") == "httpx"
    assert client_label("node") == "node"
    assert client_label(None) == "unknown"
    assert client_label("Mozilla/5.0") == "other"
