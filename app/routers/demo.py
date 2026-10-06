"""Inspección del helpdesk en memoria (solo roles con helpdesk de demo; nunca en producción)."""

from fastapi import APIRouter, Depends, HTTPException

from app.container import Components, get_components
from app.domain import Ticket

router = APIRouter(prefix="/demo", tags=["demo"])


@router.get("/tickets/{ticket_id}", response_model=Ticket)
async def get_ticket(ticket_id: int, components: Components = Depends(get_components)) -> Ticket:
    ticket = await components.helpdesk.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket no encontrado.")
    return ticket
