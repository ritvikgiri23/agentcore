from fastapi import APIRouter

from app.api.v1 import API_V1_PREFIX, auth, health, runs, sessions

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(sessions.router)
api_router.include_router(runs.router)
