from fastapi import APIRouter, Depends

from app.container import Components, get_components, require_service_token
from app.domain import IncidentRequest, IncidentResult, VisionRequest, VisionResult

router = APIRouter(prefix="/specialists", tags=["specialists"], dependencies=[Depends(require_service_token)])


@router.post("/vision/diagnose", response_model=VisionResult)
async def diagnose_visual(request: VisionRequest, components: Components = Depends(get_components)) -> VisionResult:
    return await components.vision.diagnose(request)


@router.post("/logs/analyze-incident", response_model=IncidentResult)
async def analyze_incident(request: IncidentRequest, components: Components = Depends(get_components)) -> IncidentResult:
    return await components.logs.analyze(request)
