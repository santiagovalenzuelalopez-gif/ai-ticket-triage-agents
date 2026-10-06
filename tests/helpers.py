import asyncio
import os
from datetime import datetime
from pathlib import Path

from app.core.config import Settings
from app.domain import (
    Article,
    Classification,
    ImageAttachment,
    IncidentRequest,
    IncidentResult,
    Report,
    Ticket,
    TicketType,
    VisionRequest,
    VisionResult,
)
from app.llm import RuleBasedLLM

NOW = datetime(2026, 3, 1, 12, 0, 0)  # reloj fijo: los tests no dependen de la hora de la máquina
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def make_settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(logs_dir=str(tmp_path / "logs"), webhook_token="tok", **overrides)


def ticket(ticket_id=1, body="Necesito ayuda con algo general", subject="Consulta", state="new", company="Acme",
           articles=None) -> Ticket:
    return Ticket(
        id=ticket_id, title=subject, state=state, customer="user@example.com", customer_company=company,
        articles=articles if articles is not None else [Article(sender="customer", subject=subject, body=body)],
    )


def fatal_line(when: datetime, message="Call to undefined function render()", file="/var/www/app.php", line=10, n=1) -> str:
    return (
        f'{when:%Y/%m/%d %H:%M:%S} [error] 1#1: *{n} FastCGI sent in stderr: "PHP message: PHP Fatal error:  '
        f'{message} in {file}:{line}" while reading response header from upstream, client: 10.0.0.{n}, '
        f'server: www.acme.example, request: "GET /x HTTP/1.1"'
    )


def write_log(directory: Path, name: str, lines: list[str], mtime: datetime = NOW) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


# --- Dobles ---------------------------------------------------------------------------------


class FakeVision:
    def __init__(self, delay=0.0, fail=False):
        self.calls: list[VisionRequest] = []
        self.delay, self.fail = delay, fail

    async def diagnose(self, request: VisionRequest) -> VisionResult:
        self.calls.append(request)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("vision caído")
        return VisionResult(status="ok", diagnosis="El banner tiene bajo contraste.", findings=[{"title": "Contraste"}])


class FakeLogs:
    def __init__(self, delay=0.0, found=2, fail=False):
        self.calls: list[IncidentRequest] = []
        self.delay, self.found, self.fail = delay, found, fail

    async def analyze(self, request: IncidentRequest) -> IncidentResult:
        self.calls.append(request)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("logs caído")
        return IncidentResult(
            ticket_id=request.ticket_id, entity=request.entity, logs_found=self.found,
            summary="Se encontraron errores fatales.", evidence=["LINEA CRUDA 1", "LINEA CRUDA 2"],
        )


class ScriptedLLM(RuleBasedLLM):
    """LLM simulado con la clasificación y el reporte fijados por el test."""

    def __init__(self, classification: Classification | None = None, report_type=None, boom=False):
        self._classification, self._report_type, self._boom = classification, report_type, boom

    async def classify(self, text, faq_snippets):
        if self._boom:
            raise RuntimeError("modelo caído")
        return self._classification or await super().classify(text, faq_snippets)

    async def write_report(self, text, specialist_inputs, faq_snippets):
        report = await super().write_report(text, specialist_inputs, faq_snippets)
        return Report(type_id=self._report_type, diagnosis=report.diagnosis, faq_reference=report.faq_reference)


def visual_and_critical() -> Classification:
    return Classification(category="visual", type_id=TicketType.SERVICE_REQUEST, criticality=10, reasoning="ambos")


def image() -> ImageAttachment:
    return ImageAttachment(data=PNG, mime_type="image/png", filename="boceto.png")
