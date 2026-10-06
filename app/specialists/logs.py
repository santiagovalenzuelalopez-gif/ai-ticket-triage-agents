"""Especialista de logs: busca errores fatales de una entidad y los diagnostica."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.domain import IncidentRequest, IncidentResult, LogDiagnosis, LogEntry
from app.llm import TriageLLM
from app.specialists.log_parser import parse_line
from app.specialists.log_sources import LogSource, entity_token

logger = logging.getLogger(__name__)

EVIDENCE_LINES = 5
EVIDENCE_MAX_CHARS = 800
SUMMARY_ITEMS = 5
LLM_CONCURRENCY = 4


class LogsSpecialist:
    def __init__(
        self,
        llm: TriageLLM,
        source: LogSource,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ):
        self._llm = llm
        self._source = source
        self._settings = settings
        self._tz = ZoneInfo(settings.logs_timezone)
        # Inyectable: los tests fijan la hora y no dependen del reloj de la máquina.
        self._clock = clock or (lambda: datetime.now(self._tz))

    async def analyze(self, request: IncidentRequest) -> IncidentResult:
        window = self._settings.logs_window_hours
        token = entity_token(request.entity)
        if token is None:
            logger.warning("logs_entity_rejected", extra={"ticket_id": request.ticket_id, "entity": repr(request.entity)})
            return self._empty(request, "No se pudo determinar una entidad válida para buscar en los logs.")

        raw_lines = await asyncio.to_thread(self._source.search_fatal_errors, token, window)
        entries = [e for e in (parse_line(line) for line in raw_lines) if e]

        # La ventana de N horas de la búsqueda aplica a la fecha de modificación del ARCHIVO, no a cada
        # línea: un archivo escrito de forma continua también devuelve errores viejos. Se filtra por el
        # timestamp real de cada entrada, en la zona horaria del servidor de logs (los timestamps son
        # naive en hora local de ese servidor, no en UTC).
        cutoff = self._clock().replace(tzinfo=None) - timedelta(hours=window)
        in_window = sorted((e for e in entries if e.timestamp >= cutoff), key=lambda e: e.timestamp)
        logger.info("logs_analyzed", extra={"ticket_id": request.ticket_id, "token": token, "raw": len(raw_lines),
                                            "parsed": len(entries), "in_window": len(in_window)})
        if not in_window:
            return self._empty(request, f"No se encontraron errores fatales relacionados con '{request.entity}' "
                                        f"en las últimas {window} horas.")

        diagnoses = await self._diagnose(request, in_window)
        return IncidentResult(
            ticket_id=request.ticket_id,
            entity=request.entity,
            logs_found=len(in_window),
            diagnoses=diagnoses,
            summary=self._summary(request.entity, in_window, diagnoses),
            evidence=self._evidence(in_window),
        )

    async def _diagnose(self, request: IncidentRequest, entries: list[LogEntry]) -> list[LogDiagnosis]:
        if len(entries) > self._settings.consolidate_above:
            # Muchos errores: un único diagnóstico consolidado (una llamada, no cientos)
            consolidated = await self._llm.diagnose_logs_batch(request.title, request.ticket_text, entries)
            return [consolidated.model_copy(update={"summary": f"[CONSOLIDADO {len(entries)} ERRORES] {consolidated.summary}"})]
        semaphore = asyncio.Semaphore(LLM_CONCURRENCY)

        async def one(entry: LogEntry) -> LogDiagnosis:
            async with semaphore:
                return await self._llm.diagnose_log(entry)

        return list(await asyncio.gather(*(one(e) for e in entries)))

    @staticmethod
    def _summary(entity: str | None, entries: list[LogEntry], diagnoses: list[LogDiagnosis]) -> str:
        lines = [f"Se encontraron {len(entries)} errores fatales de '{entity}':"]
        for index, diagnosis in enumerate(diagnoses[:SUMMARY_ITEMS], start=1):
            lines.append(f"{index}. {diagnosis.error_type}: {diagnosis.summary}")
        if len(diagnoses) > SUMMARY_ITEMS:
            lines.append(f"... y {len(diagnoses) - SUMMARY_ITEMS} más.")
        lines.append(f"Recomendación principal: {diagnoses[0].recommendation}")
        return "\n".join(lines)

    @staticmethod
    def _evidence(entries: list[LogEntry]) -> list[str]:
        """Líneas crudas que respaldan el diagnóstico: sin duplicados, las más recientes, en orden cronológico."""
        seen, picked = set(), []
        for entry in reversed(entries):
            raw = entry.raw.strip()
            key = raw[:200]
            if not raw or key in seen:
                continue
            seen.add(key)
            picked.append(raw if len(raw) <= EVIDENCE_MAX_CHARS else raw[:EVIDENCE_MAX_CHARS] + " […]")
            if len(picked) >= EVIDENCE_LINES:
                break
        return list(reversed(picked))

    @staticmethod
    def _empty(request: IncidentRequest, summary: str) -> IncidentResult:
        return IncidentResult(ticket_id=request.ticket_id, entity=request.entity, logs_found=0, summary=summary)
