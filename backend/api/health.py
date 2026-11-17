from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from backend.monitoring.readiness import ReadinessProbe

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, bool] = {}


@router.get("/live")
async def live() -> HealthResponse:
    """The process is running and serving requests."""
    return HealthResponse(status="ok")


@router.get("/ready")
async def ready(request: Request, response: Response) -> HealthResponse:
    """All dependencies are usable. Returns 503 otherwise."""
    probe: ReadinessProbe = request.app.state.readiness
    report = await probe.report()
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="unavailable", checks=report.checks)
    return HealthResponse(status="ok", checks=report.checks)
