"""Vetted response capabilities — the ONLY actions a response playbook can name.

A playbook references a capability by name and can reference nothing else;
resolution of an unknown name fails closed at author time (schema validation)
AND at execution time (the outbox handler). Phase 1 registers tier-0 only:
actions whose blast radius is a case annotation or an operator-configured
notification. Endpoint-impacting capabilities (block/disable/isolate) are
deliberately absent until the approval plane is proven (#49 phasing).

Capability classes reuse the ``core.ir`` taxonomy (``CapabilityClass``,
``ApprovalPolicy``). Tier-0 capabilities are explicitly AUTONOMOUS even where
the class default would gate them (``notify_webhook`` is ``write_external`` →
default ``typed_reason``): the override is justified because the target is an
operator-configured notification connector — SocTalk hands a signed envelope to
an endpoint the MSSP chose, it does not act on customer infrastructure. Any
future capability that mutates external state MUST NOT copy this override.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.ir.events import canonical_json
from soctalk.core.ir.models import CapabilityClass
from soctalk.core.ir.tools import ApprovalPolicy

logger = structlog.get_logger()

# HTTP timeout for the webhook connector. Short on purpose: the executor drains
# the outbox serially per claim and a slow endpoint must not stall the queue —
# retries with backoff are the outbox's job, not a long client timeout's.
_WEBHOOK_TIMEOUT_SECONDS = 10.0
_MAX_NOTE_BODY = 4096

SIGNATURE_HEADER = "X-SocTalk-Signature"
DELIVERY_HEADER = "X-SocTalk-Delivery"


def sign_webhook_body(secret: str, body: bytes) -> str:
    """``sha256=<hex hmac>`` over the exact request bytes — the receiver
    recomputes on the raw body, so the payload must be serialized once and
    signed as sent (no re-serialization between sign and post)."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def assert_webhook_url_allowed(url: str) -> None:
    """SSRF floor on the connector target (Codex #49 High-3).

    The URL comes from tenant policy, not the playbook — but an AUTONOMOUS
    ``write_external`` capability POSTing from L1 must still refuse to reach
    into the cluster: https only (``SOCTALK_RESPONSE_WEBHOOK_ALLOW_HTTP=1`` is
    the documented local-dev escape hatch), and every resolved address must be
    globally routable — private, loopback, link-local, reserved, multicast and
    unspecified ranges all reject. DNS re-resolution between this check and
    the POST is accepted residual risk at this tier; a vetted-connector
    allowlist is the phase-2 fix.
    """
    import ipaddress
    import os
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    allow_http = os.getenv("SOCTALK_RESPONSE_WEBHOOK_ALLOW_HTTP", "").lower() in (
        "1", "true", "yes",
    )
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise ValueError("response_webhook_url must be https")
    host = parts.hostname or ""
    if not host:
        raise ValueError("response_webhook_url has no host")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"response_webhook_url host does not resolve: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise ValueError(
                f"response_webhook_url resolves to non-global address {addr} — refused"
            )


# Handler contract: (db, tenant_id, payload) -> external_ref | None.
# ``payload`` is the outbox row's payload: {envelope, playbook, capability,
# params, delivery}. Raise to fail the action (the outbox retries per its
# max_attempts budget); return the remote reference for the audit chain.
Handler = Callable[[AsyncSession, UUID, dict[str, Any]], Awaitable[str | None]]


@dataclass(frozen=True)
class ResponseCapability:
    name: str
    capability_class: CapabilityClass
    approval: ApprovalPolicy
    description: str
    handler: Handler


async def _annotate_investigation(
    db: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
) -> str | None:
    """Tier-0: write a system note on the investigation. Local DB write only."""
    envelope = payload.get("envelope") or {}
    params = payload.get("params") or {}
    playbook = payload.get("playbook") or {}
    investigation_id = envelope.get("investigation_id")
    if not investigation_id:
        raise ValueError("envelope missing investigation_id")
    body = str(params.get("body") or "").strip()
    if not body:
        # An empty annotation is an authoring bug — say what fired anyway so
        # the note still carries the audit value the playbook intended.
        body = "response playbook fired"
    suffix = (
        f"\n\n[response playbook {playbook.get('id')}@v{playbook.get('version')} "
        f"on {envelope.get('disposition')}]"
    )
    await db.execute(
        text(
            "INSERT INTO notes (id, tenant_id, investigation_id, author_kind, "
            "                   author_id, body, visibility) "
            "VALUES (:id, :t, :c, 'system', :a, :b, 'mssp_only')"
        ),
        {
            "id": str(uuid4()),
            "t": str(tenant_id),
            "c": str(investigation_id),
            "a": f"response:{playbook.get('id')}",
            "b": (body + suffix)[:_MAX_NOTE_BODY],
        },
    )
    return None


async def _notify_webhook(
    db: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
) -> str | None:
    """Tier-0: POST the signed envelope to the tenant's configured webhook.

    This is the generic external-dispatch connector (#49): the envelope is the
    interop contract, the receiving SOAR/case-manager owns everything after the
    handoff. Config comes from tenant policy rows (``response_webhook_url``,
    optional ``response_webhook_secret``) — no URL in the playbook itself, so a
    playbook author can request a notification but never choose its target.
    """
    import httpx

    from soctalk.core.ir.policies import effective_policy

    policy = await effective_policy(db, tenant_id)
    url = str(policy.get("response_webhook_url") or "").strip()
    if not url:
        raise ValueError(
            "response_webhook_url tenant policy is missing — configure the "
            "connector before activating a notify_webhook playbook"
        )
    assert_webhook_url_allowed(url)
    body_obj = {
        "envelope": payload.get("envelope") or {},
        "playbook": payload.get("playbook") or {},
        "params": payload.get("params") or {},
    }
    body = canonical_json(body_obj).encode()
    headers = {
        "Content-Type": "application/json",
        DELIVERY_HEADER: str(payload.get("delivery") or ""),
    }
    secret = str(policy.get("response_webhook_secret") or "")
    if secret:
        headers[SIGNATURE_HEADER] = sign_webhook_body(secret, body)
    async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, content=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"webhook returned HTTP {resp.status_code}")
    # The remote's request id (when offered) beats a bare status code as the
    # audit chain's external reference.
    remote_ref = resp.headers.get("X-Request-Id") or resp.headers.get("Request-Id")
    return remote_ref or f"http:{resp.status_code}"


def _resolve_endpoint(policy: dict[str, Any], endpoint_id: str) -> dict[str, Any]:
    """Resolve an operator-configured action endpoint by id from tenant policy.

    The playbook names an endpoint id + action, NEVER a URL — the operator owns
    the id→url/secret mapping in the ``response_action_endpoints`` policy, so a
    playbook author can request an action but can never choose its target
    (the SSRF/exfil boundary, #49). Fails closed on an unknown/blank id."""
    registry = policy.get("response_action_endpoints") or {}
    if not isinstance(registry, dict):
        raise ValueError("response_action_endpoints policy is malformed")
    cfg = registry.get(endpoint_id)
    if not isinstance(cfg, dict) or not str(cfg.get("url") or "").strip():
        raise ValueError(
            f"response action endpoint {endpoint_id!r} is not configured — "
            "an operator must define it in response_action_endpoints policy"
        )
    return cfg


async def _external_action(
    db: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
) -> str | None:
    """Gated: POST the signed envelope + named action to an operator-configured
    action endpoint (the generic external-action connector, #49 phase 2).

    Unlike ``notify_webhook`` (a single tenant webhook, tier-0), this resolves
    one of many named endpoints and carries a named ``action`` — the seam where
    concrete stack behavior (disable a Wazuh endpoint) lives OUTSIDE core, behind
    the response-action contract. WRITE_EXTERNAL + TYPED_REASON: it is never
    autonomous, so it only ever runs after a human approves the routed proposal.
    """
    import httpx

    from soctalk.core.ir.policies import effective_policy

    params = payload.get("params") or {}
    endpoint_id = str(params.get("endpoint") or "").strip()
    action = str(params.get("action") or "").strip()
    if not endpoint_id or not action:
        raise ValueError("external_action requires params.endpoint and params.action")

    policy = await effective_policy(db, tenant_id)
    cfg = _resolve_endpoint(policy, endpoint_id)
    url = str(cfg["url"]).strip()
    assert_webhook_url_allowed(url)

    body_obj = {
        "action": action,
        "envelope": payload.get("envelope") or {},
        "playbook": payload.get("playbook") or {},
        "params": {k: v for k, v in params.items() if k not in ("endpoint", "action")},
    }
    body = canonical_json(body_obj).encode()
    headers = {
        "Content-Type": "application/json",
        DELIVERY_HEADER: str(payload.get("delivery") or ""),
    }
    secret = str(cfg.get("secret") or "")
    if secret:
        headers[SIGNATURE_HEADER] = sign_webhook_body(secret, body)
    async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, content=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"external action endpoint returned HTTP {resp.status_code}")
    remote_ref = resp.headers.get("X-Request-Id") or resp.headers.get("Request-Id")
    return remote_ref or f"http:{resp.status_code}"


RESPONSE_CAPABILITIES: dict[str, ResponseCapability] = {
    cap.name: cap
    for cap in (
        ResponseCapability(
            name="annotate_investigation",
            capability_class=CapabilityClass.WRITE_SANDBOX,
            approval=ApprovalPolicy.AUTONOMOUS,
            description="Write a system note on the investigation (local only).",
            handler=_annotate_investigation,
        ),
        ResponseCapability(
            name="notify_webhook",
            capability_class=CapabilityClass.WRITE_EXTERNAL,
            approval=ApprovalPolicy.AUTONOMOUS,
            description=(
                "POST the signed disposition envelope to the tenant's configured "
                "webhook connector (generic external SOAR handoff)."
            ),
            handler=_notify_webhook,
        ),
        ResponseCapability(
            name="external_action",
            capability_class=CapabilityClass.WRITE_EXTERNAL,
            # Gated: never autonomous. Routes to a human-approved proposal
            # before the executor calls the operator-configured endpoint.
            approval=ApprovalPolicy.TYPED_REASON,
            description=(
                "POST a named action + signed envelope to an operator-configured "
                "action endpoint (concrete stack behavior lives outside core)."
            ),
            handler=_external_action,
        ),
    )
}

# A close is the suppression-shaped direction (#43): response actions on
# on_close stay annotation/audit-only until outbox idempotency and the
# approval plane are proven in production (#49 non-negotiables).
ON_CLOSE_ALLOWED: frozenset[str] = frozenset({"annotate_investigation"})
