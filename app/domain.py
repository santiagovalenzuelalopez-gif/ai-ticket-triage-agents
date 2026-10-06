"""Modelos del dominio: tickets, clasificación y resultados de los especialistas."""

from datetime import datetime
from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TicketType(IntEnum):
    INCIDENT = 10
    SERVICE_REQUEST = 14
    INQUIRY = 19


TYPE_LABELS = {
    TicketType.INCIDENT: "Incidente",
    TicketType.SERVICE_REQUEST: "Requerimiento",
    TicketType.INQUIRY: "Consulta",
}
VALID_TYPE_IDS = {int(t) for t in TicketType}


# --- Helpdesk -----------------------------------------------------------------


class Article(BaseModel):
    sender: Literal["customer", "agent", "system"]
    subject: str = ""
    body: str = ""


class ImageAttachment(BaseModel):
    data: str = Field(..., description="Imagen en base64")
    mime_type: str = "image/png"
    filename: str = "captura.png"


class Ticket(BaseModel):
    id: int
    title: str
    state: str = "new"
    customer: str | None = None
    customer_company: str | None = None
    articles: list[Article] = Field(default_factory=list)


# --- Clasificación -------------------------------------------------------------


class Classification(BaseModel):
    category: Literal["visual", "incident", "inquiry"] = "inquiry"
    type_id: TicketType = TicketType.INQUIRY
    criticality: int = Field(default=0, ge=0, le=10)
    is_security_alert: bool = False
    reasoning: str = ""

    @property
    def requires_visual(self) -> bool:
        return self.category == "visual"


class Report(BaseModel):
    type_id: int | None = None
    diagnosis: str
    faq_reference: str | None = None


# --- Especialista visual -----------------------------------------------------------


class VisionRequest(BaseModel):
    ticket_id: str | None = None
    ticket_text: str = Field(..., min_length=1, max_length=20000)
    images: list[ImageAttachment] = Field(default_factory=list)


class VisionResult(BaseModel):
    status: Literal["ok", "error"]
    diagnosis: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    skipped_images: int = 0
    error: str | None = None


# --- Especialista de logs --------------------------------------------------------------


class LogEntry(BaseModel):
    timestamp: datetime
    level: str
    message: str
    client_ip: str | None = None
    request: str | None = None
    file: str | None = None
    line: int | None = None
    raw: str


class LogDiagnosis(BaseModel):
    error_type: str
    severity: Literal["crítica", "alta", "media", "baja"]
    summary: str
    probable_cause: str
    recommendation: str
    urgent: bool = False


class IncidentRequest(BaseModel):
    ticket_id: str
    title: str = ""
    ticket_text: str = ""
    entity: str | None = None


class IncidentResult(BaseModel):
    ticket_id: str
    entity: str | None
    logs_found: int
    diagnoses: list[LogDiagnosis] = Field(default_factory=list)
    summary: str
    evidence: list[str] = Field(default_factory=list)  # líneas crudas que respaldan el diagnóstico
