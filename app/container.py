"""Composición según ``SERVICE_ROLE`` (única parte que conoce las implementaciones concretas)."""

import hmac
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, Header, HTTPException

from app.clients import (
    GoogleIdTokenProvider,
    HttpLogsClient,
    HttpVisionClient,
    InProcessLogsClient,
    InProcessVisionClient,
    NoAuth,
    StaticTokenProvider,
)
from app.core.config import Settings, get_settings
from app.domain import ImageAttachment, Ticket
from app.helpdesk import MemoryHelpdesk
from app.knowledge import FaqKnowledgeBase
from app.llm import GeminiLLM, RuleBasedLLM, TriageLLM
from app.orchestrator import TriageService
from app.specialists.log_sources import LocalDirLogSource, LogSource, SshLogSource
from app.specialists.logs import LogsSpecialist
from app.specialists.vision import VisionSpecialist

logger = logging.getLogger(__name__)

DEMO_TICKETS_PATH = "data/demo_tickets.json"


@dataclass
class Components:
    settings: Settings
    triage: TriageService | None = None
    vision: VisionSpecialist | None = None
    logs: LogsSpecialist | None = None
    helpdesk: MemoryHelpdesk | None = None


def build_llm(settings: Settings) -> TriageLLM:
    return GeminiLLM(settings) if settings.llm_backend == "gemini" else RuleBasedLLM()


def build_token_provider(settings: Settings):
    if settings.service_auth == "google":
        return GoogleIdTokenProvider()
    if settings.service_auth == "static":
        return StaticTokenProvider(settings.service_token)
    return NoAuth()


def build_log_source(settings: Settings) -> LogSource:
    if settings.logs_backend == "ssh":
        return SshLogSource(settings.ssh_host, settings.ssh_port, settings.ssh_user, settings.ssh_key_path,
                            settings.ssh_known_hosts, settings.logs_dir)
    return LocalDirLogSource(settings.logs_dir)


def load_demo_helpdesk(path: str = DEMO_TICKETS_PATH) -> MemoryHelpdesk:
    file = Path(path)
    data = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {"tickets": [], "images": {}}
    tickets = [Ticket.model_validate(t) for t in data["tickets"]]
    images = {int(k): [ImageAttachment.model_validate(i) for i in v] for k, v in data.get("images", {}).items()}
    return MemoryHelpdesk(tickets, images)


def build_components(settings: Settings) -> Components:
    llm = build_llm(settings)
    role = settings.service_role
    components = Components(settings=settings)

    if role in ("all", "vision"):
        components.vision = VisionSpecialist(llm, settings)
    if role in ("all", "logs"):
        components.logs = LogsSpecialist(llm, build_log_source(settings), settings)

    if role in ("all", "orchestrator"):
        if role == "all":
            vision_client = InProcessVisionClient(components.vision)
            logs_client = InProcessLogsClient(components.logs)
        else:
            auth = build_token_provider(settings)
            vision_client = HttpVisionClient(settings.vision_url, auth) if settings.vision_url else None
            logs_client = HttpLogsClient(settings.logs_url, auth) if settings.logs_url else None
        components.helpdesk = load_demo_helpdesk()  # producción: implementar el puerto Helpdesk
        components.triage = TriageService(
            components.helpdesk, llm, FaqKnowledgeBase.from_file(settings.faq_path),
            vision_client, logs_client, settings,
        )

    if role in ("vision", "logs") and not settings.specialist_tokens:
        logger.warning("specialist_unauthenticated: sin SPECIALIST_TOKENS; se confía en la IAM de la plataforma")
    return components


@lru_cache
def get_components() -> Components:
    return build_components(get_settings())


def require_service_token(
    authorization: str | None = Header(default=None), components: Components = Depends(get_components)
) -> None:
    """Defensa en profundidad: si hay tokens de servicio configurados, se exigen (Bearer)."""
    tokens = [t.strip() for t in components.settings.specialist_tokens.split(",") if t.strip()]
    if not tokens:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not any(hmac.compare_digest(token.encode(), t.encode()) for t in tokens):
        raise HTTPException(status_code=401, detail="Token de servicio inválido.")
