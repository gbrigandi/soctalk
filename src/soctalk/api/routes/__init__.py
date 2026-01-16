"""API route modules."""

from soctalk.api.routes.auth import router as auth_router
from soctalk.api.routes.analytics import router as analytics_router
from soctalk.api.routes.audit import router as audit_router
from soctalk.api.routes.events import router as events_router
from soctalk.api.routes.investigations import router as investigations_router
from soctalk.api.routes.metrics import router as metrics_router
from soctalk.api.routes.review import router as review_router
from soctalk.api.routes.settings import router as settings_router

__all__ = [
    "auth_router",
    "analytics_router",
    "audit_router",
    "events_router",
    "investigations_router",
    "metrics_router",
    "review_router",
    "settings_router",
]
