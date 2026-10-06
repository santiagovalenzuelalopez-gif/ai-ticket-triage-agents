"""Genera un log de error de demo con fallos fatales recientes (para que entren en la ventana de búsqueda).

    python scripts/generate_demo_logs.py [directorio] [cantidad]
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ERRORS = [
    ("Allowed memory size of 134217728 bytes exhausted (tried to allocate 20480 bytes)", "/var/www/portal/src/Report.php", 214),
    ("Call to undefined function render_widget()", "/var/www/portal/themes/main/home.php", 88),
    ("Uncaught PDOException: SQLSTATE[HY000] [2002] Connection refused", "/var/www/portal/src/Db.php", 41),
]


def main(directory: str = "data/logs", count: int = 3) -> None:
    tz = ZoneInfo(os.environ.get("LOGS_TIMEZONE", "UTC"))
    now = datetime.now(tz).replace(tzinfo=None)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    lines = []
    for index in range(count):
        message, file, line = ERRORS[index % len(ERRORS)]
        stamp = (now - timedelta(minutes=5 * (count - index))).strftime("%Y/%m/%d %H:%M:%S")
        lines.append(
            f'{stamp} [error] 1234#1234: *{index + 1} FastCGI sent in stderr: "PHP message: PHP Fatal error:  '
            f'{message} in {file}:{line}" while reading response header from upstream, client: 10.0.0.{index + 1}, '
            f'server: www.acme.example, request: "GET /home HTTP/1.1"'
        )
    (target / "www.acme.error.log.1").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{count} errores de demo escritos en {target / 'www.acme.error.log.1'}")


if __name__ == "__main__":
    main(*(sys.argv[1:2]), *(int(a) for a in sys.argv[2:3]))
