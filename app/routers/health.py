from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/version")
async def version():
    settings = get_settings()
    return {
        "service": settings.service_name,
        "version": settings.service_version,
        "environment": settings.environment,
    }
