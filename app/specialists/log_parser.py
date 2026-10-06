"""Parseo de logs de error de Nginx/PHP en entradas estructuradas."""

import re
from datetime import datetime

from app.domain import LogEntry

# 2026/03/01 10:15:32 [error] 123#123: <mensaje>
_LINE_RE = re.compile(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+\d+#\d+:\s+(.+)")
_CLIENT_RE = re.compile(r"client:\s+([\d.]+)")
_REQUEST_RE = re.compile(r'request:\s+"([^"]+)"')
_PHP_FILE_RE = re.compile(r"in\s+(/[^\s:]+):(\d+)")
# grep puede anteponer "archivo:" cuando hay varios ficheros: nos quedamos desde el timestamp
_TIMESTAMP_START_RE = re.compile(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} .*")


def parse_line(line: str) -> LogEntry | None:
    line = line.strip()
    if not line:
        return None
    start = _TIMESTAMP_START_RE.search(line)
    if start:
        line = start.group(0)
    match = _LINE_RE.match(line)
    if not match:
        return None

    stamp, level, message = match.groups()
    try:
        timestamp = datetime.strptime(stamp, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None  # una fecha inválida no se "inventa": la línea se descarta

    client = _CLIENT_RE.search(message)
    request = _REQUEST_RE.search(message)
    php_file = _PHP_FILE_RE.search(message)
    return LogEntry(
        timestamp=timestamp,
        level=level,
        message=message,
        client_ip=client.group(1) if client else None,
        request=request.group(1) if request else None,
        file=php_file.group(1) if php_file else None,
        line=int(php_file.group(2)) if php_file else None,
        raw=line,
    )


def parse_many(content: str) -> list[LogEntry]:
    """Parsea varias líneas; las que no empiezan con timestamp continúan la anterior (stack traces)."""
    entries: list[LogEntry] = []
    current = ""
    for line in content.strip().splitlines():
        if _LINE_RE.match(line.strip()):
            if current and (entry := parse_line(current)):
                entries.append(entry)
            current = line
        elif current:
            current += " " + line.strip()
    if current and (entry := parse_line(current)):
        entries.append(entry)
    return entries
