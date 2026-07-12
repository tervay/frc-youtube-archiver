"""API routers aggregated under /api."""

from fastapi import APIRouter

from . import actions, ganymede, queue, settings, sources, sse, stats, videos

api_router = APIRouter(prefix="/api")
api_router.include_router(stats.router)
api_router.include_router(videos.router)
api_router.include_router(queue.router)
api_router.include_router(sources.router)
api_router.include_router(settings.router)
api_router.include_router(actions.router)
api_router.include_router(ganymede.router)
api_router.include_router(sse.router)
