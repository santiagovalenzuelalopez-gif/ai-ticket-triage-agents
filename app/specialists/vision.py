"""Especialista visual: analiza las imágenes adjuntas a un ticket."""

import logging

from app.core.config import Settings
from app.domain import VisionRequest, VisionResult
from app.llm import TriageLLM, decode_images

logger = logging.getLogger(__name__)

ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}


class VisionSpecialist:
    def __init__(self, llm: TriageLLM, settings: Settings):
        self._llm = llm
        self._settings = settings

    async def diagnose(self, request: VisionRequest) -> VisionResult:
        """Nunca lanza: un fallo se devuelve como ``status="error"`` para que el orquestador degrade."""
        try:
            candidates = [i for i in request.images if i.mime_type in ALLOWED_MIME][: self._settings.max_images]
            decoded, invalid = decode_images(candidates)
            oversized = [d for d in decoded if len(d[0]) > self._settings.max_image_bytes]
            decoded = [d for d in decoded if len(d[0]) <= self._settings.max_image_bytes]
            skipped = len(request.images) - len(decoded)
            if invalid or oversized:
                logger.warning("vision_images_skipped", extra={"invalid": invalid, "oversized": len(oversized)})

            if not decoded:
                return VisionResult(status="ok", diagnosis="No se recibieron imágenes válidas para analizar.",
                                    skipped_images=skipped)

            summary, findings = await self._llm.describe_images(request.ticket_text, decoded)
            return VisionResult(status="ok", diagnosis=summary, findings=findings, skipped_images=skipped)
        except Exception as exc:  # noqa: BLE001
            logger.error("vision_failed", exc_info=True, extra={"ticket_id": request.ticket_id})
            return VisionResult(status="error", error=type(exc).__name__)
