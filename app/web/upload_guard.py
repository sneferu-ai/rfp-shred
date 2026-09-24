"""Outer ASGI admission guard for the multipart upload endpoint.

The CSRF dependency must inspect multipart form data, so upload limits that
live inside the route run too late.  This middleware sits outside FastAPI's
dependency stack, rejects known-oversize requests without reading a byte,
and counts streamed/chunked bodies while they are consumed.
"""

from __future__ import annotations

import json
import threading

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MULTIPART_OVERHEAD_BYTES = 64 * 1024
DEFAULT_CONCURRENT_UPLOAD_BODIES = 2


class _RequestBodyTooLarge(Exception):
    """Internal control flow raised by the guarded ASGI receive callable."""


class UploadBodyAdmissionMiddleware:
    """Reject oversized or excess concurrent upload bodies before parsing.

    Admission is deliberately non-blocking: once ``max_concurrent`` bodies
    are in flight, another upload receives a retryable 503 instead of tying
    up another request waiting for capacity.  The bound is per application
    process, matching the resource boundary that holds parsed upload bytes.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        max_concurrent: int = DEFAULT_CONCURRENT_UPLOAD_BODIES,
        upload_path: str = "/app/new",
    ) -> None:
        if max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.max_concurrent = max_concurrent
        self.upload_path = upload_path
        self._active = 0
        self._active_lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._guards(scope):
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_request_bytes:
            await _send_problem(
                send,
                status=413,
                code="UPLOAD_TOO_LARGE",
                title="Upload too large",
                detail="File exceeds the maximum upload size",
            )
            return

        if not self._try_admit():
            await _send_problem(
                send,
                status=503,
                code="UPLOAD_CAPACITY_BUSY",
                title="Upload capacity busy",
                detail="Another upload is still arriving; retry shortly",
                retry_after="1",
            )
            return

        received = 0

        async def guarded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_request_bytes:
                    raise _RequestBodyTooLarge
            return message

        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, tracked_send)
        except (_RequestBodyTooLarge, BaseExceptionGroup) as exc:
            # Starlette's BaseHTTP middleware may preserve the receive error
            # inside an ExceptionGroup.  Only translate a group when every
            # nested failure is our size sentinel; unrelated failures must
            # retain their original traceback and server-error semantics.
            if not _is_body_limit_failure(exc):
                raise
            # Upload routes consume the body before constructing a response,
            # so the normal path can still return a precise 413.  Preserve
            # the original exception if a future endpoint starts responding
            # before it finishes reading: ASGI cannot start a second response.
            if response_started:
                raise
            await _send_problem(
                send,
                status=413,
                code="UPLOAD_TOO_LARGE",
                title="Upload too large",
                detail="File exceeds the maximum upload size",
            )
        finally:
            self._release()

    def _guards(self, scope: Scope) -> bool:
        return scope.get("method", "").upper() == "POST" and scope.get("path") == self.upload_path

    def _try_admit(self) -> bool:
        with self._active_lock:
            if self._active >= self.max_concurrent:
                return False
            self._active += 1
            return True

    def _release(self) -> None:
        with self._active_lock:
            self._active -= 1


def _content_length(scope: Scope) -> int | None:
    """Return a trustworthy non-negative Content-Length, if one exists.

    Missing, malformed, negative, or duplicated values fall back to streamed
    byte counting.  That keeps chunked uploads protected without trusting an
    ambiguous header.
    """

    values = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
    if len(values) != 1:
        return None
    try:
        parsed = int(values[0])
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _is_body_limit_failure(exc: BaseException) -> bool:
    if isinstance(exc, _RequestBodyTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_is_body_limit_failure(item) for item in exc.exceptions)
    return False


async def _send_problem(
    send: Send,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    retry_after: str | None = None,
) -> None:
    payload = json.dumps(
        {
            "type": f"urn:rfp-shred:error:{code.lower().replace('_', '-')}",
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/problem+json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", retry_after.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
