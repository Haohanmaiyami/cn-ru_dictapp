"""Request correlation without logging user text, query strings or client IPs.

This middleware does not replace Uvicorn/Caddy's own access-log configuration.
Unhandled exceptions retain Starlette's existing handling; an outer 500 response
may not contain the header, although its request is logged here with status 500.
"""
from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger("uvicorn.error")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"})


class RequestDiagnosticsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid4())
        # Never accept an incoming correlation value: it could contain user data.
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # FastAPI adds a route object after matching; .path is the declared
            # template, never the request URL or concrete path parameter value.
            route = getattr(scope.get("route"), "path", None)
            if not isinstance(route, str):
                route = "unmatched"
            method = scope.get("method")
            logger.info(json.dumps({
                "event": "http_request",
                "request_id": request_id,
                "method": method if method in _METHODS else "OTHER",
                "route": route,
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }, ensure_ascii=True, separators=(",", ":")))
