"""Fuentes de logs: directorio local (demo) o servidor remoto por SSH.

La "entidad" que se busca sale del texto de un ticket, es decir, de un tercero. Cualquier valor que
termine en un nombre de archivo, un patrón de ``find`` o un comando remoto se trata como hostil:
se reduce a un token estricto y se entrecomilla; si no cumple, no se busca.
"""

import re
import shlex
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

# Un token válido: minúsculas, dígitos y guiones. Sin comillas, espacios, '$', ';', '`', '*', etc.
ENTITY_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MAX_LINES = 400
FATAL_MARKER = "PHP Fatal error"


def entity_token(entity: str | None) -> str | None:
    """'Grupo Acme' -> 'acme' (última palabra, sin acentos). None si no es un token seguro."""
    ascii_name = unicodedata.normalize("NFKD", entity or "").encode("ascii", "ignore").decode().lower().strip()
    if not ascii_name:
        return None
    token = ascii_name.split()[-1]
    return token if ENTITY_TOKEN_RE.match(token) else None


class LogSource(Protocol):
    def search_fatal_errors(self, token: str, hours: int) -> list[str]:
        """Líneas con errores fatales de los archivos de log de ``token`` modificados en las últimas ``hours``."""


class LocalDirLogSource:
    """Mismo criterio que la búsqueda remota, sobre un directorio local."""

    def __init__(self, directory: str, now: Callable[[], float] = time.time):
        self._dir = Path(directory)
        self._now = now

    def search_fatal_errors(self, token: str, hours: int) -> list[str]:
        if not ENTITY_TOKEN_RE.match(token):
            raise ValueError("token de entidad inválido")
        cutoff = self._now() - hours * 3600
        lines: list[str] = []
        for path in self._dir.glob("**/*"):
            name = path.name.lower()
            relative_depth = len(path.relative_to(self._dir).parts)
            if not path.is_file() or relative_depth > 2:
                continue
            if token not in name or "error.log" not in name or "preprod" in str(path).lower():
                continue
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines.extend(line.rstrip("\n") for line in handle if FATAL_MARKER in line)
        return sorted(lines)[-MAX_LINES:]


def build_find_command(directory: str, token: str, hours: int) -> str:
    """Comando remoto. Todo valor dinámico va entrecomillado con ``shlex.quote`` y, además, el token
    se valida antes: dos capas, porque una falla de cualquiera de las dos sería ejecución remota."""
    if not ENTITY_TOKEN_RE.match(token):
        raise ValueError("token de entidad inválido")
    minutes = int(hours) * 60
    return (
        f"cd {shlex.quote(directory)} 2>/dev/null && "
        f"find . -maxdepth 2 -type f -mmin -{minutes} "
        f"-iname {shlex.quote(f'*{token}*')} -iname '*error.log*' -not -path '*preprod*' -print0 2>/dev/null | "
        f"xargs -0 -r grep -aEh {shlex.quote(FATAL_MARKER)} 2>/dev/null | sort | tail -n {MAX_LINES} || true"
    )


class SshLogSource:
    def __init__(self, host: str, port: int, user: str, key_path: str, known_hosts: str, directory: str):
        if not (host and user and known_hosts):
            raise ValueError("SSH_HOST, SSH_USER y SSH_KNOWN_HOSTS son obligatorios con LOGS_BACKEND=ssh")
        self._host, self._port, self._user = host, port, user
        self._key_path, self._known_hosts, self._dir = key_path, known_hosts, directory

    def search_fatal_errors(self, token: str, hours: int) -> list[str]:
        import paramiko

        command = build_find_command(self._dir, token, hours)
        client = paramiko.SSHClient()
        client.load_host_keys(self._known_hosts)
        # RejectPolicy: solo se confía en hosts ya registrados. AutoAddPolicy aceptaría la clave de
        # cualquiera (incluido un atacante en medio) y entregaría el acceso a los logs.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                self._host, port=self._port, username=self._user, key_filename=self._key_path or None,
                timeout=10, banner_timeout=10, auth_timeout=10, look_for_keys=False, allow_agent=False,
            )
            _, stdout, _ = client.exec_command(command, timeout=30)  # noqa: S601 - comando validado y entrecomillado
            output = stdout.read().decode("utf-8", errors="replace")
        finally:
            client.close()
        return [line.strip() for line in output.splitlines() if line.strip()]
