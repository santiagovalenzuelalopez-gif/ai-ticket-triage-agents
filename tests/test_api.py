"""Contrato HTTP: webhook, especialistas expuestos y cliente HTTP real contra el servidor real."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients import HttpLogsClient, HttpVisionClient, call_with_budget
from app.container import build_components, get_components
from app.domain import IncidentRequest, VisionRequest
from app.main import app
from app.routers.webhook import extract_ticket_id
from tests.helpers import NOW, PNG, fatal_line, make_settings, write_log

HEADERS = {"X-Webhook-Token": "tok"}


@pytest.fixture
def components(tmp_path):
    write_log(tmp_path / "logs", "www.acme.example.error.log.1", [fatal_line(NOW, message="Allowed memory size exhausted")])
    settings = make_settings(tmp_path, logs_window_hours=24 * 365 * 10)  # ventana amplia: el reloj real no es NOW
    return build_components(settings)


@pytest.fixture
def client(components):
    app.dependency_overrides[get_components] = lambda: components
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_webhook_requires_the_shared_secret(client):
    assert client.post("/webhooks/tickets", json={"ticket_id": 1}).status_code == 401
    assert client.post("/webhooks/tickets", json={"ticket_id": 1}, headers={"X-Webhook-Token": "mal"}).status_code == 401


def test_webhook_rejects_payloads_without_a_ticket_id(client):
    assert client.post("/webhooks/tickets", json={"nada": 1}, headers=HEADERS).status_code == 422
    assert client.post("/webhooks/tickets", content=b"no es json", headers=HEADERS).status_code == 422


def test_webhook_accepts_immediately_and_triages_in_background(client):
    response = client.post("/webhooks/tickets", json={"ticket_id": 1}, headers=HEADERS)
    assert response.status_code == 202 and response.json() == {"status": "accepted", "ticket_id": 1}

    articles = client.get("/demo/tickets/1").json()["articles"]
    system = [a for a in articles if a["sender"] == "system"]
    assert len(system) == 1 and "Olvidé mi contraseña" in system[0]["body"]


def test_end_to_end_incident_uses_the_log_specialist(client, components):
    client.post("/webhooks/tickets", json={"Event": {"TicketID": 2}}, headers=HEADERS)
    posted = components.helpdesk.posted[0]
    assert "insumos: análisis de logs" in posted.body
    assert "Allowed memory size exhausted" in posted.body  # la evidencia del log llega al ticket
    assert posted.type_id == 10


def test_end_to_end_visual_ticket_with_attachment(client, components):
    client.post("/webhooks/tickets", json={"ticket_id": 3}, headers=HEADERS)
    assert "insumos: análisis visual" in components.helpdesk.posted[0].body


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"ticket_id": 5}, 5), ({"TicketID": "7"}, 7), ({"Event": {"TicketID": 9}}, 9), ({"ticket": {"ticket_id": 4}}, 4),
     ({"ticket_id": 0}, None), ({"ticket_id": "abc"}, None), ({"ticket_id": -3}, None), ({}, None)],
)
def test_extract_ticket_id(payload, expected):
    assert extract_ticket_id(payload) == expected


def test_specialists_enforce_service_tokens_when_configured(client, components, monkeypatch):
    body = {"ticket_text": "mira", "images": [{"data": PNG, "mime_type": "image/png", "filename": "a.png"}]}
    assert client.post("/specialists/vision/diagnose", json=body).status_code == 200  # sin tokens: abierto (demo)

    monkeypatch.setattr(components.settings, "specialist_tokens", "secreto-1,secreto-2")
    assert client.post("/specialists/vision/diagnose", json=body).status_code == 401
    assert client.post("/specialists/vision/diagnose", json=body, headers={"Authorization": "Bearer mal"}).status_code == 401
    assert client.post("/specialists/vision/diagnose", json=body, headers={"Authorization": "Bearer secreto-2"}).status_code == 200


# --- Clientes HTTP ---------------------------------------------------------------------------------------


class FixedToken:
    async def headers(self, audience):
        self.audience = audience
        return {"Authorization": "Bearer id-token-de-servicio"}


async def test_http_clients_speak_the_real_specialist_contract(components):
    """El cliente HTTP del orquestador contra el servidor real de los especialistas (ASGI en memoria)."""
    app.dependency_overrides[get_components] = lambda: components
    try:
        transport = httpx.ASGITransport(app=app)
        provider = FixedToken()
        logs = await HttpLogsClient("http://logs.internal/", provider, transport).analyze(
            IncidentRequest(ticket_id="2", title="Falla", ticket_text="x", entity="Acme"))
        vision = await HttpVisionClient("http://vision.internal", provider, transport).diagnose(
            VisionRequest(ticket_text="x", images=[{"data": PNG, "mime_type": "image/png", "filename": "a.png"}]))
    finally:
        app.dependency_overrides.clear()

    assert logs.logs_found == 1 and "Allowed memory size" in logs.evidence[0]
    assert vision.status == "ok" and vision.findings
    assert provider.audience == "http://vision.internal"  # audiencia = URL base, sin barra final


async def test_http_client_sends_the_service_identity_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok", "diagnosis": "listo"})

    result = await HttpVisionClient("http://v", FixedToken(), httpx.MockTransport(handler)).diagnose(
        VisionRequest(ticket_text="hola"))
    assert seen["auth"] == "Bearer id-token-de-servicio" and seen["path"] == "/specialists/vision/diagnose"
    assert seen["body"]["ticket_text"] == "hola" and result.diagnosis == "listo"


async def test_remote_error_degrades_to_an_error_outcome():
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    client = HttpVisionClient("http://v", FixedToken(), transport)
    outcome = await call_with_budget(client.diagnose(VisionRequest(ticket_text="x")), 1.0, "vision")
    assert outcome.status == "error" and outcome.result is None


async def test_call_with_budget_times_out_slow_specialists():
    async def slow():
        await asyncio.sleep(1)

    assert (await call_with_budget(slow(), 0.05, "x")).status == "timeout"


async def test_static_token_provider_and_wiring(tmp_path):
    from app.clients import GoogleIdTokenProvider, NoAuth, StaticTokenProvider
    from app.container import build_token_provider

    assert await StaticTokenProvider("abc").headers("http://x") == {"Authorization": "Bearer abc"}
    assert isinstance(build_token_provider(make_settings(tmp_path)), NoAuth)
    assert isinstance(build_token_provider(make_settings(tmp_path, service_auth="static", service_token="t")), StaticTokenProvider)
    assert GoogleIdTokenProvider is not None
