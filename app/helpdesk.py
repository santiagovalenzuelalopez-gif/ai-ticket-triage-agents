"""Puerto hacia el sistema de tickets (helpdesk) y un adaptador en memoria para demo y tests.

Para integrar un helpdesk real basta implementar ``Helpdesk``: leer el ticket con sus artículos,
traer las imágenes adjuntas y publicar el diagnóstico como una nota del sistema.
"""

from dataclasses import dataclass
from typing import Protocol

from app.domain import Article, ImageAttachment, Ticket

# Asunto de la nota que publica el sistema. También es la marca de idempotencia:
# si el ticket ya la tiene, no se vuelve a diagnosticar.
DIAGNOSIS_SUBJECT = "Diagnóstico automático"


class Helpdesk(Protocol):
    async def get_ticket(self, ticket_id: int) -> Ticket | None: ...

    async def get_images(self, ticket_id: int, max_images: int, max_bytes: int) -> list[ImageAttachment]: ...

    async def post_diagnosis(self, ticket_id: int, subject: str, body: str, type_id: int | None) -> None:
        """Publica una nota del sistema y, si ``type_id`` no es None, actualiza el tipo del ticket."""


@dataclass
class PostedDiagnosis:
    ticket_id: int
    subject: str
    body: str
    type_id: int | None


class MemoryHelpdesk:
    def __init__(self, tickets: list[Ticket] | None = None, images: dict[int, list[ImageAttachment]] | None = None):
        self._tickets = {t.id: t for t in (tickets or [])}
        self._images = images or {}
        self.posted: list[PostedDiagnosis] = []
        self.types: dict[int, int] = {}

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        ticket = self._tickets.get(ticket_id)
        return ticket.model_copy(deep=True) if ticket else None

    async def get_images(self, ticket_id: int, max_images: int, max_bytes: int) -> list[ImageAttachment]:
        selected = []
        for image in self._images.get(ticket_id, []):
            if len(image.data) * 3 // 4 > max_bytes:  # tamaño aproximado del binario
                continue
            selected.append(image)
            if len(selected) >= max_images:
                break
        return selected

    async def post_diagnosis(self, ticket_id: int, subject: str, body: str, type_id: int | None) -> None:
        self._tickets[ticket_id].articles.append(Article(sender="system", subject=subject, body=body))
        if type_id is not None:
            self.types[ticket_id] = type_id
        self.posted.append(PostedDiagnosis(ticket_id, subject, body, type_id))
