from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.middleware import CorrelationMiddleware
from app.routers import demo, health, specialists, webhook

settings = get_settings()
setup_logging(settings.log_level)

app = FastAPI(
    title="AI Ticket Triage Agents",
    description="Orquestador de triaje de tickets con IA y especialistas (visual, logs). Un código, tres desplegables.",
    version=settings.service_version,
)
app.add_middleware(CorrelationMiddleware)
app.include_router(health.router)

role = settings.service_role
if role in ("all", "orchestrator"):
    app.include_router(webhook.router)
    app.include_router(demo.router)
if role in ("all", "vision", "logs"):
    app.include_router(specialists.router)
