import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from app.container import Components, get_components

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])

_ID_KEYS = ("ticket_id", "TicketID", "ticketId")
_NESTED = ("event", "Event", "ticket", "Ticket")


def extract_ticket_id(payload: dict) -> int | None:
    """El id puede llegar plano o anidado según el helpdesk; se acepta una lista cerrada de formas."""
    candidates = [payload] + [payload[k] for k in _NESTED if isinstance(payload.get(k), dict)]
    for scope in candidates:
        for key in _ID_KEYS:
            try:
                value = int(scope[key])
            except (KeyError, TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


@router.post("/webhooks/tickets", status_code=202)
async def ticket_webhook(
    request: Request,
    background: BackgroundTasks,
    x_webhook_token: str | None = Header(default=None),
    components: Components = Depends(get_components),
) -> dict:
    """Responde 202 de inmediato y procesa el triaje en segundo plano: el helpdesk no espera a la IA."""
    expected = components.settings.webhook_token
    if not x_webhook_token or not hmac.compare_digest(x_webhook_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Token de webhook inválido.")

    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    ticket_id = extract_ticket_id(payload if isinstance(payload, dict) else {})
    if ticket_id is None:
        raise HTTPException(status_code=422, detail="El payload no incluye un ticket_id válido.")

    background.add_task(components.triage.process, ticket_id)
    return {"status": "accepted", "ticket_id": ticket_id}
