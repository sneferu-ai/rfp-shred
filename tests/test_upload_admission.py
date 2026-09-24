"""Outer upload admission: pre-parser size and slow-body concurrency bounds."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.web.app import create_app
from app.web.upload_guard import UploadBodyAdmissionMiddleware
from tests.conftest import csrf_headers, signup_json


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/app/new",
        "raw_path": b"/app/new",
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _status(messages: list[dict]) -> int:
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


def _headers(messages: list[dict]) -> dict[str, str]:
    raw = next(message["headers"] for message in messages if message["type"] == "http.response.start")
    return {name.decode(): value.decode() for name, value in raw}


def _problem(messages: list[dict]) -> dict:
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return json.loads(body)


def test_declared_oversize_is_rejected_before_downstream_parser() -> None:
    entered_parser = False
    received = False

    async def parser_app(scope, receive, send):
        nonlocal entered_parser
        entered_parser = True
        await receive()

    async def receive():
        nonlocal received
        received = True
        return {"type": "http.request", "body": b"not read", "more_body": False}

    async def exercise():
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        guard = UploadBodyAdmissionMiddleware(parser_app, max_request_bytes=100)
        await guard(_scope([(b"content-length", b"101")]), receive, send)
        return sent

    sent = asyncio.run(exercise())
    assert _status(sent) == 413
    assert _problem(sent)["code"] == "UPLOAD_TOO_LARGE"
    assert _headers(sent)["content-type"] == "application/problem+json"
    assert _headers(sent)["cache-control"] == "no-store"
    assert entered_parser is False
    assert received is False


def test_chunked_body_without_content_length_is_counted_and_bounded() -> None:
    chunks = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56", "more_body": False},
        ]
    )
    parser_reads = 0

    async def parser_app(scope, receive, send):
        nonlocal parser_reads
        parser_reads += 1
        await receive()
        parser_reads += 1
        await receive()

    async def receive():
        return next(chunks)

    async def exercise():
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        guard = UploadBodyAdmissionMiddleware(parser_app, max_request_bytes=5)
        await guard(_scope([(b"transfer-encoding", b"chunked")]), receive, send)
        return sent

    sent = asyncio.run(exercise())
    assert parser_reads == 2
    assert _status(sent) == 413
    assert _problem(sent)["code"] == "UPLOAD_TOO_LARGE"
    assert _headers(sent)["cache-control"] == "no-store"


def test_chunked_multipart_limit_survives_the_real_middleware_stack(settings, tmp_path) -> None:
    """Regression for the pure-ASGI guard around FastAPI's BaseHTTP layer."""
    small_settings = replace(
        settings,
        database_url=f"sqlite:///{tmp_path}/chunked.db",
        files_dir=str(tmp_path / "chunked-files"),
        max_upload_mb=0,
    )
    app = create_app(small_settings)
    with TestClient(app) as isolated_client:
        signup_json(isolated_client)
        headers = csrf_headers(isolated_client)
        headers.update(
            {
                "Content-Type": "multipart/form-data; boundary=slow-boundary",
                "Transfer-Encoding": "chunked",
            }
        )
        prefix = (
            b"--slow-boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="large.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n"
        )
        chunks = iter([prefix, b"x" * (64 * 1024), b"\r\n--slow-boundary--\r\n"])
        response = isolated_client.post("/app/new", content=chunks, headers=headers)

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "UPLOAD_TOO_LARGE"


def test_second_slow_body_is_rejected_without_waiting_or_entering_parser() -> None:
    async def exercise():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        parser_entries = 0
        first_sent: list[dict] = []
        second_sent: list[dict] = []

        async def parser_app(scope, receive, send):
            nonlocal parser_entries
            parser_entries += 1
            first_entered.set()
            await receive()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def slow_receive():
            await release_first.wait()
            return {"type": "http.request", "body": b"one", "more_body": False}

        async def second_receive():
            return {"type": "http.request", "body": b"two", "more_body": False}

        async def first_send(message):
            first_sent.append(message)

        async def second_send(message):
            second_sent.append(message)

        guard = UploadBodyAdmissionMiddleware(
            parser_app,
            max_request_bytes=100,
            max_concurrent=1,
        )
        first_task = asyncio.create_task(guard(_scope(), slow_receive, first_send))
        await asyncio.wait_for(first_entered.wait(), timeout=1)

        # This call must complete immediately; it must neither wait behind the
        # slow sender nor invoke the multipart/parser application a second time.
        await asyncio.wait_for(guard(_scope(), second_receive, second_send), timeout=1)
        assert parser_entries == 1
        assert _status(second_sent) == 503
        assert _problem(second_sent)["code"] == "UPLOAD_CAPACITY_BUSY"
        assert _headers(second_sent)["cache-control"] == "no-store"
        assert _headers(second_sent)["retry-after"] == "1"

        release_first.set()
        await asyncio.wait_for(first_task, timeout=1)
        assert _status(first_sent) == 204

    asyncio.run(exercise())
