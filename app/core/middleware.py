import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# (trace_id, span_id, correlation_id) del request en curso; lo lee logging.py para
# que cada línea de log lleve el mismo contexto de correlación.
_trace_context: ContextVar[tuple[str | None, str | None, str | None]] = ContextVar(
    "_trace_context", default=(None, None, None)
)

# X-Cloud-Trace-Context: "<trace-id>/<span-id>;o=<flags>"
_GCP_TRACE_RE = re.compile(r"^([0-9a-f]+)/(\d+)(?:;o=\d)?$")
# traceparent (W3C): "00-<trace-id 32 hex>-<span-id 16 hex>-<flags>"
_W3C_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def get_trace_context() -> tuple[str | None, str | None, str | None]:
    """(trace_id, span_id, correlation_id) del request en curso, o (None, None, None)."""
    return _trace_context.get()


def parse_trace(headers) -> tuple[str | None, str | None]:
    gcp_header = headers.get("X-Cloud-Trace-Context")
    if gcp_header:
        match = _GCP_TRACE_RE.match(gcp_header)
        if match:
            return match.group(1), match.group(2)

    traceparent = headers.get("traceparent")
    if traceparent:
        match = _W3C_TRACEPARENT_RE.match(traceparent)
        if match:
            return match.group(1), match.group(2)

    return None, None


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Genera/propaga X-Correlation-ID, extrae el trace GCP/W3C y loguea request_completed."""

    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        trace_id, span_id = parse_trace(request.headers)

        token = _trace_context.set((trace_id, span_id, correlation_id))
        start = time.monotonic()
        try:
            response: Response = await call_next(request)
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            logger.info(
                "request_completed",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        finally:
            _trace_context.reset(token)
