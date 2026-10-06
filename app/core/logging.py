import json
import logging
import sys
from datetime import UTC, datetime

from app.core.middleware import get_trace_context

# Todo lo que llegue vía logger.info(msg, extra={...}) y no sea un atributo estándar
# de LogRecord se mezcla como campo estructurado en el JSON de salida.
_RESERVED_RECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Logs JSON a stdout, compatibles con la ingesta estructurada de Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "timestamp": datetime.now(UTC).isoformat(),
            "logger": record.name,
        }

        trace_id, span_id, correlation_id = get_trace_context()
        if trace_id:
            payload["logging.googleapis.com/trace"] = trace_id
        if span_id:
            payload["logging.googleapis.com/spanId"] = span_id
        if correlation_id:
            payload["correlation_id"] = correlation_id

        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
            }
        )

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Redirige el logger raíz (y los de uvicorn/gunicorn) a stdout en JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "gunicorn", "gunicorn.access", "gunicorn.error"):
        noisy = logging.getLogger(name)
        noisy.handlers = [handler]
        noisy.propagate = False
