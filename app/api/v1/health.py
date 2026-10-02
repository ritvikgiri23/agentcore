from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health", summary="Liveness check")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
