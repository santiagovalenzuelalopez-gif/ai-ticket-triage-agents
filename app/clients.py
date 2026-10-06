"""Cómo el orquestador llega a los especialistas: en el mismo proceso o por HTTP.

Ambas variantes cumplen el mismo contrato, por lo que el orquestador no sabe (ni le importa) si los
especialistas son módulos o servicios separados: se elige al desplegar con ``SERVICE_ROLE``.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel

from app.domain import IncidentRequest, IncidentResult, VisionRequest, VisionResult
from app.specialists.logs import LogsSpecialist
from app.specialists.vision import VisionSpecialist

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class VisionClient(Protocol):
    async def diagnose(self, request: VisionRequest) -> VisionResult: ...


class LogsClient(Protocol):
    async def analyze(self, request: IncidentRequest) -> IncidentResult: ...


@dataclass
class SpecialistOutcome:
    """Resultado de invocar a un especialista con presupuesto de tiempo."""

    status: Literal["ok", "timeout", "error"]
    result: BaseModel | None = None


async def call_with_budget(coro, timeout: float, name: str) -> SpecialistOutcome:
    """Un especialista lento o caído NO debe impedir el diagnóstico: se degrada y se sigue."""
    try:
        return SpecialistOutcome("ok", await asyncio.wait_for(coro, timeout=timeout))
    except TimeoutError:
        logger.warning("specialist_timeout", extra={"specialist": name, "timeout": timeout})
        return SpecialistOutcome("timeout")
    except Exception:  # noqa: BLE001
        logger.error("specialist_failed", exc_info=True, extra={"specialist": name})
        return SpecialistOutcome("error")


# --- En proceso -----------------------------------------------------------------------------


class InProcessVisionClient:
    def __init__(self, specialist: VisionSpecialist):
        self._specialist = specialist

    async def diagnose(self, request: VisionRequest) -> VisionResult:
        return await self._specialist.diagnose(request)


class InProcessLogsClient:
    def __init__(self, specialist: LogsSpecialist):
        self._specialist = specialist

    async def analyze(self, request: IncidentRequest) -> IncidentResult:
        return await self._specialist.analyze(request)


# --- HTTP (servicios separados) -------------------------------------------------------------------


class TokenProvider(Protocol):
    async def headers(self, audience: str) -> dict[str, str]: ...


class NoAuth:
    async def headers(self, audience: str) -> dict[str, str]:
        return {}


class StaticTokenProvider:
    """Bearer fijo: entornos locales y docker-compose. En Cloud Run se usa GoogleIdTokenProvider."""

    def __init__(self, token: str):
        self._headers = {"Authorization": f"Bearer {token}"}

    async def headers(self, audience: str) -> dict[str, str]:
        return dict(self._headers)


class GoogleIdTokenProvider:
    """ID token OIDC para invocar un servicio privado de Cloud Run: ``audience`` es su URL base."""

    def __init__(self) -> None:
        from google.auth.transport import requests as google_requests

        self._request = google_requests.Request()

    async def headers(self, audience: str) -> dict[str, str]:
        from google.oauth2 import id_token

        try:
            token = await asyncio.to_thread(id_token.fetch_id_token, self._request, audience)
            return {"Authorization": f"Bearer {token}"}
        except Exception:  # noqa: BLE001
            # Sin token la llamada fallará con 401/403 en destino y el motivo queda en el log.
            logger.warning("id_token_unavailable", exc_info=True, extra={"audience": audience})
            return {}


class _HttpClient:
    def __init__(self, base_url: str, auth: TokenProvider, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._auth = auth
        self._transport = transport

    async def _post(self, path: str, payload: BaseModel, model: type[T]) -> T:
        headers = await self._auth.headers(self._base)
        async with httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            response = await client.post(f"{self._base}{path}", content=payload.model_dump_json(),
                                         headers={"Content-Type": "application/json", **headers})
        response.raise_for_status()
        return model.model_validate(response.json())


class HttpVisionClient(_HttpClient):
    async def diagnose(self, request: VisionRequest) -> VisionResult:
        return await self._post("/specialists/vision/diagnose", request, VisionResult)


class HttpLogsClient(_HttpClient):
    async def analyze(self, request: IncidentRequest) -> IncidentResult:
        return await self._post("/specialists/logs/analyze-incident", request, IncidentResult)
