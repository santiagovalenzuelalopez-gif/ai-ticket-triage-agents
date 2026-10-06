"""Orquestador del triaje: decide qué especialistas intervienen y publica un único diagnóstico.

    ticket nuevo -> guardas -> clasificar (con FAQ) -> especialistas EN PARALELO (con presupuesto)
                -> redactar diagnóstico final -> publicar nota (+ tipo)
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

from app.clients import LogsClient, SpecialistOutcome, VisionClient, call_with_budget
from app.core.config import Settings
from app.domain import (
    TYPE_LABELS,
    VALID_TYPE_IDS,
    Article,
    IncidentRequest,
    IncidentResult,
    Ticket,
    TicketType,
    VisionRequest,
    VisionResult,
)
from app.helpdesk import DIAGNOSIS_SUBJECT, Helpdesk
from app.knowledge import FaqKnowledgeBase
from app.llm import TriageLLM

logger = logging.getLogger(__name__)

MAX_FINDINGS_CHARS = 15_000


@dataclass
class TriageOutcome:
    ticket_id: int
    status: Literal["diagnosed", "skipped", "error"]
    reason: str = ""
    type_id: int | None = None
    inputs: list[str] | None = None


def relevant_text(articles: list[Article], max_chars: int) -> str:
    """Último mensaje que no sea del sistema (si no hay, el primero), acotado al presupuesto de prompt."""
    human = [a for a in articles if a.sender != "system"]
    last = human[-1] if human else articles[0]
    return f"Asunto: {last.subject}\nMensaje: {last.body}"[:max_chars]


class TriageService:
    def __init__(
        self,
        helpdesk: Helpdesk,
        llm: TriageLLM,
        knowledge: FaqKnowledgeBase,
        vision: VisionClient | None,
        logs: LogsClient | None,
        settings: Settings,
    ):
        self._helpdesk, self._llm, self._kb = helpdesk, llm, knowledge
        self._vision, self._logs, self._settings = vision, logs, settings
        # Los webhooks se reintentan y a veces se duplican: un ticket no se procesa dos veces a la vez.
        self._inflight: set[int] = set()

    async def process(self, ticket_id: int) -> TriageOutcome:
        if ticket_id in self._inflight:
            return TriageOutcome(ticket_id, "skipped", "already_in_progress")
        self._inflight.add(ticket_id)
        try:
            return await self._process(ticket_id)
        except Exception:
            logger.error("triage_failed", exc_info=True, extra={"ticket_id": ticket_id})
            return TriageOutcome(ticket_id, "error", "unexpected_error")
        finally:
            self._inflight.discard(ticket_id)

    async def _process(self, ticket_id: int) -> TriageOutcome:
        settings = self._settings
        ticket = await self._helpdesk.get_ticket(ticket_id)
        if ticket is None:
            return TriageOutcome(ticket_id, "skipped", "ticket_not_found")
        if skip := self._guard(ticket):
            return TriageOutcome(ticket_id, "skipped", skip)

        text = relevant_text(ticket.articles, settings.max_text_chars)
        snippets = self._kb.search(text)
        classification = await self._llm.classify(text, snippets)
        critical = classification.criticality >= settings.critical_threshold or classification.is_security_alert

        # Especialistas EN PARALELO, cada uno con su presupuesto de tiempo
        jobs: dict[str, asyncio.Future] = {}
        if classification.requires_visual and self._vision:
            images = await self._helpdesk.get_images(ticket_id, settings.max_images, settings.max_image_bytes)
            request = VisionRequest(ticket_id=str(ticket_id), ticket_text=text, images=images)
            jobs["vision"] = asyncio.ensure_future(
                call_with_budget(self._vision.diagnose(request), settings.vision_timeout_seconds, "vision"))
        if (classification.category == "incident" or critical) and self._logs:
            entity = await self._llm.extract_entity(ticket, text)
            request = IncidentRequest(ticket_id=str(ticket_id), title=ticket.title, ticket_text=text, entity=entity)
            jobs["logs"] = asyncio.ensure_future(
                call_with_budget(self._logs.analyze(request), settings.logs_timeout_seconds, "logs"))
        outcomes: dict[str, SpecialistOutcome] = dict(zip(jobs, await asyncio.gather(*jobs.values()), strict=True))

        inputs, used, evidence, findings = self._collect(outcomes)
        report = await self._llm.write_report(text, inputs, snippets)

        # El diagnóstico final es la última palabra sobre el tipo (ya vio los insumos); un valor
        # inventado por el modelo no se envía: se conserva el de la clasificación.
        final_type = int(report.type_id) if report.type_id in VALID_TYPE_IDS else int(classification.type_id)
        body = self._compose(report.diagnosis, final_type, critical, evidence, findings, used)

        # Kill switch: con la asignación automática apagada se calcula el tipo (y se escribe en el
        # texto, para que el equipo lo asigne a mano) pero no se modifica el ticket.
        sent_type = final_type if settings.ticket_type_enabled else None
        await self._helpdesk.post_diagnosis(ticket_id, DIAGNOSIS_SUBJECT, body, sent_type)
        logger.info("triage_done", extra={"ticket_id": ticket_id, "category": classification.category,
                                          "type_id": final_type, "critical": critical, "inputs": used})
        return TriageOutcome(ticket_id, "diagnosed", type_id=final_type, inputs=used)

    def _guard(self, ticket: Ticket) -> str | None:
        if any(a.sender == "system" and a.subject == DIAGNOSIS_SUBJECT for a in ticket.articles):
            return "already_diagnosed"  # idempotencia: el webhook puede dispararse más de una vez
        if ticket.state != self._settings.new_ticket_state:
            return "ticket_not_new"
        if not ticket.articles:
            return "no_articles"
        if len(ticket.articles) > self._settings.max_articles:
            return "too_many_articles"  # ya hay una conversación: no se pisa el trabajo humano
        return None

    @staticmethod
    def _collect(outcomes: dict[str, SpecialistOutcome]):
        inputs: list[str] = []
        used: list[str] = []
        evidence: list[str] = []
        findings: list[dict] = []

        if (vision := outcomes.get("vision")) and isinstance(vision.result, VisionResult) and vision.result.status == "ok":
            inputs.append(f"[INSUMO VISUAL]: {vision.result.diagnosis}")
            findings = vision.result.findings
            used.append("análisis visual")

        if logs := outcomes.get("logs"):
            if isinstance(logs.result, IncidentResult):
                inputs.append(f"[INSUMO TÉCNICO LOGS]: {logs.result.summary}")
                evidence = logs.result.evidence
                if logs.result.logs_found:  # "no se encontró nada" no cuenta como insumo
                    used.append("análisis de logs")
            elif logs.status == "timeout":
                inputs.append("[INSUMO TÉCNICO LOGS]: análisis de logs omitido por latencia.")
        return inputs, used, evidence, findings

    @staticmethod
    def _compose(diagnosis: str, type_id: int, critical: bool, evidence: list[str], findings: list[dict], used: list[str]) -> str:
        label = TYPE_LABELS.get(TicketType(type_id), str(type_id))
        body = diagnosis
        if critical:
            body = "🚨 [ALERTA CRÍTICA] PROTOCOLO DE EMERGENCIA ACTIVADO\n" + body
        body += f"\n\nTipo de ticket: {label}"
        # Verbatim, DESPUÉS de lo que redactó el modelo: es la evidencia, no una paráfrasis.
        if evidence:
            body += "\n\n---\nLog(s) consultado(s) como soporte:\n" + "\n".join(f"- {line}" for line in evidence)
        if findings:
            text = json.dumps(findings, ensure_ascii=False, indent=2)
            if len(text) > MAX_FINDINGS_CHARS:
                text = text[:MAX_FINDINGS_CHARS] + "\n... (truncado)"
            body += f"\n\n---\nHallazgos del análisis visual:\n{text}"
        header = "[Diagnóstico automático" + (f" · insumos: {', '.join(used)}" if used else "") + "]"
        return f"{header}\n\n{body}"

