from __future__ import annotations

import contextvars
import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    return request_id_var.get()


def bind_request_id(request_id: str | None) -> contextvars.Token:
    return request_id_var.set(request_id or "-")


def outbound_request_headers() -> dict[str, str]:
    request_id = current_request_id()
    return {"X-Request-ID": request_id} if request_id != "-" else {}


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        supplied = (request.headers.get("x-request-id") or "").strip()
        request_id = supplied[:128] if supplied else str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


def configure_request_logging() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"
        ))
