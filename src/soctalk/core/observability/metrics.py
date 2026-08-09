"""Prometheus exporter for SocTalk V1.

See docs/multi-tenant/security-model.md. §10: tenant isolation extends to metrics: per-tenant counters carry a ``tenant_id`` label so MSSPs can attribute usage
and detect per-tenant outliers. Install-level metrics (no tenant label) cover
SocTalk's own health.

Exposes ``GET /metrics`` via a FastAPI router; mount it on the API app at
install time.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Per-tenant counters / gauges
# ---------------------------------------------------------------------------

tenant_events_ingested = Counter(
    "soctalk_tenant_events_ingested_total",
    "Alerts / events ingested into SocTalk, per tenant.",
    labelnames=["tenant_id"],
)

claims_denied_total = Counter(
    "soctalk_claims_denied_total",
    "Run claims refused, by reason. A tenant blocked on its daily ceiling used "
    "to be indistinguishable from an idle queue (#129); this is what makes the "
    "difference visible without reading logs.",
    ["reason"],
)

tenant_investigations_opened = Counter(
    "soctalk_tenant_investigations_opened_total",
    "Investigations opened by the orchestrator, per tenant.",
    labelnames=["tenant_id"],
)

tenant_investigations_closed = Counter(
    "soctalk_tenant_investigations_closed_total",
    "Investigations closed, per tenant, by disposition.",
    labelnames=["tenant_id", "disposition"],
)

tenant_pending_reviews = Gauge(
    "soctalk_tenant_pending_reviews",
    "Currently pending HIL reviews, per tenant.",
    labelnames=["tenant_id"],
)

tenant_llm_tokens_used = Counter(
    "soctalk_tenant_llm_tokens_total",
    "LLM tokens consumed per tenant, split by direction.",
    labelnames=["tenant_id", "direction"],  # direction: input | output
)

tenant_adapter_heartbeat_age = Gauge(
    "soctalk_tenant_adapter_heartbeat_age_seconds",
    "Seconds since last adapter heartbeat for a tenant.",
    labelnames=["tenant_id"],
)

# ---------------------------------------------------------------------------
# Install-level (no tenant label)
# ---------------------------------------------------------------------------

install_tenants_total = Gauge(
    "soctalk_install_tenants_total",
    "Total tenants in the install, by state.",
    labelnames=["state"],
)

install_api_request_duration = Histogram(
    "soctalk_api_request_duration_seconds",
    "Latency of SocTalk API endpoints.",
    labelnames=["method", "path_template", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

install_helm_op_duration = Histogram(
    "soctalk_helm_op_duration_seconds",
    "Latency of Helm operations invoked by SocTalk controller.",
    labelnames=["op", "outcome"],  # op: install|upgrade|uninstall; outcome: ok|error
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 900),
)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

metrics_router = APIRouter(tags=["observability"])


@metrics_router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


__all__ = [
    "install_api_request_duration",
    "install_helm_op_duration",
    "install_tenants_total",
    "metrics_router",
    "tenant_adapter_heartbeat_age",
    "tenant_events_ingested",
    "tenant_investigations_closed",
    "tenant_investigations_opened",
    "tenant_llm_tokens_used",
    "tenant_pending_reviews",
]
