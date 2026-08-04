"""V1 FastAPI application factory.

Composes the V1 routers (tenants, branding, LLM config, adapter, health,
metrics) and wires the identity middleware appropriate for the install's
``SOCTALK_AUTH_MODE``.

This is the production API entrypoint (served as ``soctalk.core.api.app_v1:app``).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from soctalk.core.agents import api as agents_routes
from soctalk.core.api import adapter as adapter_routes
from soctalk.core.api import auth as auth_routes
from soctalk.core.api import authorization as authorization_routes
from soctalk.core.api import branding as branding_routes
from soctalk.core.api import chat as chat_routes
from soctalk.core.api import fleet_day as fleet_day_routes
from soctalk.core.api import health as health_routes
from soctalk.core.api import investigations_bridge as investigations_bridge_routes
from soctalk.core.api import ir as ir_routes
from soctalk.core.api import legacy_stubs as legacy_stubs_routes
from soctalk.core.api import llm_config as llm_routes
from soctalk.core.api import metrics_bridge as metrics_bridge_routes
from soctalk.core.api import mssp_analytics as mssp_analytics_routes
from soctalk.core.api import mssp_dashboard as mssp_dashboard_routes
from soctalk.core.api import mssp_users as mssp_users_routes
from soctalk.core.api import public_tenant as public_tenant_routes
from soctalk.core.api import run_budget as run_budget_routes
from soctalk.core.api import tenants as tenant_routes
from soctalk.core.api import users as users_routes
from soctalk.core.api import worker_runs as worker_runs_routes
from soctalk.core.auth.config import AuthMode, get_auth_mode
from soctalk.core.auth.middleware import internal_session_middleware
from soctalk.core.observability.metrics import metrics_router
from soctalk.core.provisioning import ProvisioningWorker
from soctalk.core.tenancy import db as tenancy_db
from soctalk.core.tenancy.auth import ingress_handoff_middleware

logger = structlog.get_logger()


def _worker_enabled() -> bool:
    """Gate the provisioning worker loop.

    Defaults to ON so a plain ``api`` deployment consumes the onboarding
    queue out of the box. Set ``SOCTALK_PROVISIONING_WORKER=0`` to turn
    it off (useful for CI jobs that hit the API without wanting the
    worker to claim jobs in the same process, and for dedicated worker
    replicas where a separate entrypoint owns the loop).
    """
    return os.getenv("SOCTALK_PROVISIONING_WORKER", "1") != "0"


async def _lease_reaper_loop(stop_event: asyncio.Event) -> None:
    from soctalk.core.api.worker_runs import reap_expired_leases

    sm = tenancy_db.get_mssp_sessionmaker()
    while not stop_event.is_set():
        try:
            async with sm() as session:
                async with session.begin():
                    n = await reap_expired_leases(session)
            if n:
                logger.info("case_run_leases_reaped", count=n)
        except Exception as e:  # noqa: BLE001
            logger.warning("lease_reaper_error", error=str(e))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except TimeoutError:
            pass


async def _token_renewal_loop(stop_event: asyncio.Event) -> None:
    """Re-mint tenant adapter/worker tokens before their TTL elapses.

    Runs once immediately on startup (so an api restart heals any already-
    expired tenants) then every ``SOCTALK_TOKEN_RENEWAL_INTERVAL_SECONDS``
    (default 6h — comfortably inside the 7-day adapter TTL). See
    :mod:`soctalk.core.tenancy.token_renewal` for why this exists.
    """
    from soctalk.core.provisioning.k8s import new_k8s_client
    from soctalk.core.tenancy.token_renewal import renew_agent_tokens

    default_interval = 6 * 3600.0
    try:
        interval = float(
            os.getenv("SOCTALK_TOKEN_RENEWAL_INTERVAL_SECONDS", str(default_interval))
        )
    except ValueError:
        logger.warning("token_renewal_bad_interval_env", falling_back_to=default_interval)
        interval = default_interval
    # Floor at 60s so a misconfigured 0/negative can never tight-loop Secret
    # rewrites for every tenant.
    interval = max(60.0, interval)
    sm = tenancy_db.get_mssp_sessionmaker()
    try:
        k8s = new_k8s_client()
    except Exception as e:  # noqa: BLE001 — no cluster access (e.g. local dev): skip
        logger.warning("token_renewal_disabled_no_k8s", error=str(e))
        return
    while not stop_event.is_set():
        try:
            async with sm() as session:
                n = await renew_agent_tokens(session, k8s)
            if n:
                logger.info("agent_tokens_renewed", count=n)
        except Exception as e:  # noqa: BLE001
            logger.warning("token_renewal_loop_error", error=str(e))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger.info(
        "soctalk_v1_api_start",
        install_id=os.getenv("SOCTALK_INSTALL_ID"),
        auth_mode=get_auth_mode().value,
    )

    worker: ProvisioningWorker | None = None
    worker_task: asyncio.Task | None = None
    if _worker_enabled():
        sm = tenancy_db.get_mssp_sessionmaker()
        worker = ProvisioningWorker(sm)
        worker_task = asyncio.create_task(
            worker.run_forever(), name="provisioning-worker"
        )
        logger.info("provisioning_worker_enabled")

    # Bind MCP clients (Wazuh / Cortex / TheHive / MISP) for the chat
    # agent's tool surface. Env-driven config — same vars the
    # runs-worker uses (WAZUH_URL, WAZUH_API_USERNAME / PASSWORD, …).
    # Failure is non-fatal: chat falls back to DB-only tools and the
    # agent will say "I don't have a tool for that" rather than 500.
    try:
        from soctalk.mcp import bind_clients as _bind_mcp_clients

        await _bind_mcp_clients()
        logger.info("api_mcp_clients_bound")
    except Exception as e:  # noqa: BLE001
        logger.warning("api_mcp_clients_skipped", err=str(e)[:200])

    reaper_stop = asyncio.Event()
    reaper_task = asyncio.create_task(
        _lease_reaper_loop(reaper_stop), name="lease-reaper"
    )

    # Agent-token renewal keeps long-lived tenants' adapter/worker auth from
    # silently expiring. Gated with the provisioning worker: it mutates K8s
    # Secrets, so it belongs on the same replica that owns cluster writes.
    renewal_stop = asyncio.Event()
    renewal_task: asyncio.Task | None = None
    if _worker_enabled():
        renewal_task = asyncio.create_task(
            _token_renewal_loop(renewal_stop), name="token-renewal"
        )

    # Response executor (issue #49): drains ``response_action`` rows the
    # complete_run dispatcher enqueued. SKIP LOCKED claims make it safe on
    # every replica (unlike the provisioning worker, no single-owner gate).
    # BYPASSRLS sessionmaker: the claim query spans tenants; per-row writes
    # still run under tenant_context.
    response_executor = None
    response_task: asyncio.Task | None = None
    if os.getenv("SOCTALK_RESPONSE_EXECUTOR", "1").lower() not in ("0", "false", "no"):
        from soctalk.response.executor import ResponseExecutor

        response_executor = ResponseExecutor(tenancy_db.get_mssp_sessionmaker())
        response_task = asyncio.create_task(
            response_executor.run_forever(), name="response-executor"
        )
        logger.info("response_executor_enabled")

    try:
        yield
    finally:
        if response_executor is not None and response_task is not None:
            response_executor.stop()
            try:
                await asyncio.wait_for(response_task, timeout=10)
            except TimeoutError:
                response_task.cancel()
        reaper_stop.set()
        try:
            await asyncio.wait_for(reaper_task, timeout=5)
        except TimeoutError:
            reaper_task.cancel()
        if renewal_task is not None:
            renewal_stop.set()
            try:
                await asyncio.wait_for(renewal_task, timeout=5)
            except TimeoutError:
                renewal_task.cancel()
        if worker is not None and worker_task is not None:
            await worker.stop()
            try:
                # Worker respects stop_event and should exit within one
                # poll interval; give it a bounded grace period.
                await asyncio.wait_for(worker_task, timeout=10)
            except TimeoutError:
                worker_task.cancel()
        await tenancy_db.dispose_engines()
        logger.info("soctalk_v1_api_stop")


def create_app(db_session_middleware: type | None = None) -> FastAPI:
    app = FastAPI(
        title="SocTalk V1",
        description=(
            "MSSP-deployed SOC control plane. See docs/multi-tenant/ for architecture."
        ),
        version="0.2.0",
        lifespan=_lifespan,
        # Serve the OpenAPI schema + interactive docs UNDER ``/api/`` so they
        # are reachable through the public ingress, which routes ``/api/*`` to
        # this service and everything else to the app-ui frontend. FastAPI's
        # defaults (``/openapi.json`` / ``/docs`` / ``/redoc`` at the app root)
        # land on the frontend via the ingress and 404. The prefix is NOT
        # stripped by the ingress (the same reason ``/api/auth/*`` etc. resolve
        # here), so the app must own the full ``/api/...`` path.
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    mode = get_auth_mode()

    # Middleware ordering (FastAPI applies in REVERSE add order):
    #
    #   request  → DBSessionMiddleware       (sets request.state.db)
    #            → identity middleware        (reads db, resolves identity)
    #            → handlers
    #
    # Add DB middleware LAST (outermost).
    if mode is AuthMode.INTERNAL:
        app.add_middleware(
            BaseHTTPMiddleware, dispatch=internal_session_middleware
        )
    else:
        app.add_middleware(
            BaseHTTPMiddleware, dispatch=ingress_handoff_middleware
        )
    app.add_middleware(db_session_middleware or tenancy_db.DBSessionMiddleware)

    # Mount routers.
    app.include_router(health_routes.router)
    app.include_router(metrics_router)
    app.include_router(tenant_routes.router)
    app.include_router(branding_routes.tenant_router)
    app.include_router(branding_routes.mssp_router)
    app.include_router(llm_routes.router)
    app.include_router(llm_routes.tenant_router)
    app.include_router(run_budget_routes.router)
    app.include_router(run_budget_routes.tenant_router)
    app.include_router(adapter_routes.router)
    app.include_router(authorization_routes.router)
    app.include_router(authorization_routes.mssp_router)
    app.include_router(authorization_routes.tenant_authz_router)
    app.include_router(users_routes.tenant_users_router)
    app.include_router(mssp_users_routes.mssp_users_router)
    app.include_router(worker_runs_routes.router)
    # Slug-driven tenant landing: /api/public/tenant-by-slug/{slug} —
    # no auth, returns identity + branding so the canonical UI can
    # render branded login pages and pin tenant context from a URL
    # like ``<slug>.customer.<base>``. Slugs are public-facing (DNS).
    app.include_router(public_tenant_routes.router)
    # Canonical frontend/ talks to /api/investigations expecting the
    # legacy single-tenant shape; the bridge router maps V1 cases +
    # investigation_runs into that contract so the dashboard works on
    # multi-tenant L1 without a frontend rewrite.
    app.include_router(investigations_bridge_routes.router)
    app.include_router(fleet_day_routes.router)
    app.include_router(metrics_bridge_routes.router)
    # MSSP fleet dashboard (cross-tenant queries; rendered on the L1
    # ``/`` homepage when the operator is in MSSP scope, hidden under
    # tenant pin and from customer roles).
    app.include_router(mssp_dashboard_routes.router)
    # MSSP fleet analytics (trend-shaped, longitudinal cuts; rendered
    # on ``/analytics`` under MSSP scope. Different time horizon and
    # decision type from the dashboard, NOT a tenant-wide rollup of
    # the same widgets — see mssp_analytics.py docstring).
    app.include_router(mssp_analytics_routes.router)
    # Empty-default stubs for legacy /review, /analytics, /audit,
    # /settings, /events/stream so canonical-frontend pages render
    # while their V1 bridges are still pending (P3-*).
    app.include_router(legacy_stubs_routes.router)
    # L2-agent wire protocol (bearer-token auth, no session cookie).
    app.include_router(agents_routes.router)

    # AI SOC analyst chat (per-investigation dock + global /chat).
    app.include_router(chat_routes.router)

    # Native IR (AI-led) — enabled by default, always mounted.
    app.include_router(ir_routes.mssp_investigations_router)
    app.include_router(ir_routes.tenant_investigations_router)
    app.include_router(ir_routes.tenant_engagements_router)
    app.include_router(ir_routes.alerts_router)
    app.include_router(ir_routes.proposals_router)
    app.include_router(ir_routes.integrations_router)
    app.include_router(ir_routes.engagements_router)
    app.include_router(ir_routes.playbooks_router)
    app.include_router(ir_routes.triage_policies_router)
    app.include_router(ir_routes.authored_playbooks_router)
    app.include_router(ir_routes.authored_response_playbooks_router)

    # Auth endpoints only exist in internal mode. In proxy mode, they 404.
    if mode is AuthMode.INTERNAL:
        app.include_router(auth_routes.auth_router)
        app.include_router(auth_routes.auth_admin_router)

    return app


app = create_app()
