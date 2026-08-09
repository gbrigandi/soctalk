"""Chat API — CRUD for conversations + streaming SSE turn endpoint.

The router wraps :mod:`soctalk.chat.agent` with:
* Auth: every route depends on ``current_identity``; bearer-only
  routes 401 cleanly.
* Tenant-id stamping: writes follow the explicit MSSP-write rule
  documented in ``docs/chat-interface-plan.md``. ``investigation_id``
  present → inherit; absent → caller's ``current_tenant`` pin (mssp_admin
  needs Open SOC active for global chat) or home tenant_id.
* Per-tenant daily-cap enforcement via
  :func:`soctalk.core.cost.assert_tenant_daily_cap_ok` — same ceiling as
  the worker-claim path.
* Conversation FOR UPDATE lock around streaming so parallel-tab turns
  conflict deterministically with a 409, not a duplicate-billing race.
* Request-disconnect polling so a closed tab cancels the LLM call
  within ~1s.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.chat import actions as chat_actions
from soctalk.chat import sse
from soctalk.chat.agent import (
    TurnContext,
    load_investigation_summary,
    run_turn,
)
from soctalk.core.cost import (
    assert_mssp_user_daily_cap_ok,
    assert_tenant_daily_cap_ok,
)
from soctalk.core.tenancy.auth import UserIdentity, current_identity
from soctalk.core.tenancy.decorators import require_permission_any
from soctalk.core.tenancy.permissions import Permission

logger = structlog.get_logger()

# Mount under /api so all routes are /api/chat/* — same prefix the
# other bridges use.
router = APIRouter(
    prefix="/api/chat",
    tags=["chat"],
    dependencies=[Depends(current_identity)],
)

MSSP_LEVEL_ROLES = frozenset({"platform_admin", "mssp_admin", "mssp_manager", "analyst"})

# Chat is an *operate* surface, capability-gated end to end so customer_viewer cannot drive OR read
# it. Reads are gated too: chat history can persist operational data (e.g. list_pending_reviews tool
# output as role="tool" messages), so leaving list/get auth-only would leak the review queue to a
# read-only stakeholder indirectly.
#   * list/get/create/message/delete/stop → chat access (MSSP USE_CHAT or tenant TENANT_USE_CHAT)
#   * confirm → REVIEW-DECIDE authority, NOT mere chat access — confirming a chat-proposed action
#     approves/rejects/expires a pending review (see chat.actions._ACTION_DISPATCH), so it must
#     carry the same authority as the /api/review decide endpoints. customer_viewer is denied.
_CHAT_GUARD = require_permission_any(
    (Permission.USE_CHAT, "mssp"),
    (Permission.TENANT_USE_CHAT, "tenant"),
)
_CHAT_CONFIRM_GUARD = require_permission_any(
    (Permission.REVIEW_DECIDE, "mssp"),
    (Permission.TENANT_REVIEW_DECIDE, "tenant"),
)


def _db(request: Request) -> AsyncSession:
    sess = getattr(request.state, "db", None)
    if sess is None:
        raise HTTPException(500, "db session not attached")
    return sess


@asynccontextmanager
async def _chat_db(
    request: Request, identity: UserIdentity
) -> AsyncIterator[AsyncSession]:
    """Yield the session that's correctly scoped for the caller's audience.

    * **MSSP audience** (platform_admin / mssp_admin / analyst): open a
      fresh BYPASSRLS session and commit at exit. RLS is bypassed so
      fleet conversations (``tenant_id IS NULL``) are visible AND
      writable even when the caller has an Open SOC pin that would
      otherwise pin ``app.current_tenant_id`` on the request session.
    * **Tenant audience** (customer roles, tenant_admin, etc.): yield
      the request-bound session. The middleware commits on response;
      no extra commit here. RLS keeps the caller scoped to their home
      tenant.

    Use this instead of ``_db(request)`` directly on any handler that
    reads or writes ``conversations`` / ``chat_messages``.
    """
    if identity.role in MSSP_LEVEL_ROLES:
        from soctalk.core.tenancy.db import get_mssp_sessionmaker

        sm = get_mssp_sessionmaker()
        async with sm() as sess:
            try:
                yield sess
                await sess.commit()
            except Exception:
                await sess.rollback()
                raise
    else:
        yield _db(request)


def _default_chat_model() -> str:
    """Global fallback chat model (fleet scope or an unconfigured tenant)."""
    # Default kept current — ``claude-sonnet-4-20250514`` was an early
    # alpha identifier that Anthropic retired, and shipping it as the
    # built-in default 404'd every chat turn on fresh installs. The
    # canonical Sonnet 4 series alias is ``claude-sonnet-4-6``.
    return os.getenv("SOCTALK_CHAT_MODEL", "claude-sonnet-4-6")


async def _default_chat_model_for_tenant(
    db: AsyncSession, tenant_id: UUID | None
) -> str:
    """Default model for a NEW conversation (issue #10).

    A tenant-scoped conversation opens on the TENANT's configured model so it's
    consistent with the per-tenant provider/key the chat agent resolves at turn
    time (``_tenant_chat_llm_config``) — otherwise a new chat would default to
    the global Anthropic model even for a tenant running a self-hosted backend,
    and the provider overlay would mismatch the model. Fleet scope (no tenant)
    or an unconfigured tenant falls back to the global default.
    """
    if tenant_id is not None:
        from sqlalchemy import select

        from soctalk.core.tenancy.models import IntegrationConfig

        model = (
            await db.execute(
                select(IntegrationConfig.llm_model).where(
                    IntegrationConfig.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if model:
            return model
    return _default_chat_model()


def _default_budget_dollars() -> float:
    raw = os.getenv("SOCTALK_CHAT_CONVERSATION_BUDGET", "")
    try:
        v = float(raw) if raw else 1.0
    except ValueError:
        v = 1.0
    return v if v > 0 else 1.0


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class _CreateConversationBody(BaseModel):
    investigation_id: str | None = None
    model: str | None = None
    # tenant | mssp_fleet | null. When NULL (the common case from the
    # UI's single '+ New' button), _resolve_scope_and_tenant picks the
    # right default for the caller's role: MSSP → 'mssp_fleet',
    # customer → 'tenant'. Callers can still pin a scope explicitly.
    scope: str | None = None


class _PostMessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class _ConversationOut(BaseModel):
    id: str
    # NULL for scope='mssp_fleet' conversations.
    tenant_id: str | None
    scope: str
    # Soft fleet-scope focus (set via set_fleet_focus tool). NULL when
    # unset; the API also surfaces the slug joined from the tenants
    # table for UI rendering.
    focused_tenant_id: str | None
    focused_tenant_slug: str | None
    created_by_user_id: str
    investigation_id: str | None
    title: str | None
    model_name: str
    status: str
    total_tokens: int
    total_dollars: float
    budget_dollars: float
    created_at: datetime
    last_message_at: datetime | None


class _MessageOut(BaseModel):
    id: str
    role: str
    content: dict[str, Any]
    tokens_in: int
    tokens_out: int
    dollars: float
    created_at: datetime


class _ConversationListOut(BaseModel):
    items: list[_ConversationOut]


class _ConversationDetailOut(BaseModel):
    conversation: _ConversationOut
    messages: list[_MessageOut]


# ---------------------------------------------------------------------------
# Tenant-id resolution (MSSP-write rule)
# ---------------------------------------------------------------------------


_VALID_SCOPES = frozenset({"tenant", "mssp_fleet"})


async def _resolve_scope_and_tenant(
    db: AsyncSession,
    identity: UserIdentity,
    *,
    requested_scope: str | None,
    investigation_id: UUID | None,
) -> tuple[str, UUID | None]:
    """Resolve (scope, tenant_id) for a new conversation.

    Defaulting:

    * Investigation-bound: ALWAYS ``scope='tenant'``, tenant inherited
      from the investigation row.
    * Otherwise, when ``requested_scope`` is None (the common UI path
      — single '+ New' button), pick by role:
        - MSSP-level → ``mssp_fleet`` (no pin needed; the agent uses
          ``set_fleet_focus`` to narrow within a conversation).
        - Customer → ``tenant`` (their home tenant).
    * Explicit ``requested_scope`` is honoured if valid for the role.
    """
    is_mssp = identity.role in MSSP_LEVEL_ROLES

    if requested_scope is not None and requested_scope not in _VALID_SCOPES:
        raise HTTPException(400, "invalid scope")
    if not is_mssp and requested_scope == "mssp_fleet":
        raise HTTPException(403, "fleet scope requires an MSSP-level role")

    # Investigation-bound chats are always tenant-scoped.
    if investigation_id is not None:
        row = (
            await db.execute(
                text("SELECT tenant_id::text FROM investigations WHERE id = :id"),
                {"id": str(investigation_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(404, "investigation not found")
        inv_tenant = UUID(row["tenant_id"])
        if not is_mssp and identity.tenant_id != inv_tenant:
            raise HTTPException(
                403, "investigation belongs to a different tenant"
            )
        return ("tenant", inv_tenant)

    current_pin = getattr(identity, "current_tenant", None) if is_mssp else None

    # Pick the default scope when caller didn't specify.
    # Open SOC pinning is the user-facing verb for "operate as this
    # tenant" — when pinned, a new chat is tenant-scoped to that pin;
    # otherwise MSSP users get fleet (and narrow via set_fleet_focus).
    if requested_scope is None:
        if is_mssp:
            requested_scope = "tenant" if current_pin else "mssp_fleet"
        else:
            requested_scope = "tenant"

    if requested_scope == "mssp_fleet":
        return ("mssp_fleet", None)

    # requested_scope == "tenant"
    if is_mssp:
        # MSSP tenant-scope requires the pin to know which tenant_id
        # to stamp. Drop the pin via "Clear" if you wanted fleet.
        if current_pin is None:
            raise HTTPException(
                400,
                "tenant-scope chat needs an Open SOC pin; clear the "
                "pin to start a fleet chat instead",
            )
        pin_uuid = (
            current_pin if isinstance(current_pin, UUID) else UUID(str(current_pin))
        )
        return ("tenant", pin_uuid)

    return ("tenant", identity.tenant_id)


def _title_from_text(s: str) -> str:
    s = " ".join(s.split())
    return s[:80] if len(s) <= 80 else s[:79] + "…"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/conversations",
    response_model=_ConversationOut,
    dependencies=[Depends(_CHAT_GUARD)],
)
async def create_conversation(
    body: _CreateConversationBody, request: Request
) -> _ConversationOut:
    identity = current_identity(request)

    inv_uuid: UUID | None = None
    if body.investigation_id:
        try:
            inv_uuid = UUID(body.investigation_id)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "invalid investigation_id") from e

    budget = _default_budget_dollars()

    async with _chat_db(request, identity) as db:
        scope, tenant_id = await _resolve_scope_and_tenant(
            db,
            identity,
            requested_scope=body.scope,
            investigation_id=inv_uuid,
        )
        # An explicit model choice wins; otherwise default to the tenant's own
        # model (tenant scope) so the conversation opens consistent with the
        # per-tenant provider the agent resolves at turn time.
        model_name = body.model or await _default_chat_model_for_tenant(db, tenant_id)
        row = (
            await db.execute(
                text(
                    """
                    INSERT INTO conversations (
                        tenant_id, scope, created_by_user_id,
                        investigation_id, model_name, status,
                        budget_dollars
                    )
                    VALUES (:t, :s, :u, :inv, :m, 'active', :b)
                    RETURNING id::text, tenant_id::text, scope,
                              focused_tenant_id::text,
                              NULL::text AS focused_tenant_slug,
                              created_by_user_id::text,
                              investigation_id::text, title, model_name,
                              status, total_tokens, total_dollars,
                              budget_dollars, created_at, last_message_at
                    """
                ),
                {
                    "t": str(tenant_id) if tenant_id else None,
                    "s": scope,
                    "u": str(identity.user_id),
                    "inv": str(inv_uuid) if inv_uuid else None,
                    "m": model_name,
                    "b": budget,
                },
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(500, "conversation insert failed")
    return _ConversationOut(**dict(row))


@router.get(
    "/conversations",
    response_model=_ConversationListOut,
    dependencies=[Depends(_CHAT_GUARD)],
)
async def list_conversations(
    request: Request,
    investigation_id: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> _ConversationListOut:
    identity = current_identity(request)
    conds = ["1=1"]
    params: dict[str, Any] = {"n": limit, "off": offset}
    if investigation_id:
        conds.append("investigation_id = :inv")
        params["inv"] = investigation_id
    # MSSP callers: the BYPASSRLS session would otherwise drown them in
    # cross-user rows, so we filter to their own creations AND match the
    # listing to the active scope context (pin → tenant chats for that
    # pin; no pin → fleet chats). Tenant-audience callers stay
    # RLS-scoped to their tenant and don't get a user filter (a
    # tenant_admin keeps seeing the tenant's conversations).
    if identity.role in MSSP_LEVEL_ROLES:
        conds.append("created_by_user_id = :u")
        params["u"] = str(identity.user_id)
        current_pin = getattr(identity, "current_tenant", None)
        if current_pin is not None:
            conds.append("scope = 'tenant'")
            conds.append("tenant_id = :pin")
            params["pin"] = str(current_pin)
        else:
            conds.append("scope = 'mssp_fleet'")
    sql = (
        "SELECT c.id::text, c.tenant_id::text, c.scope, "
        "c.focused_tenant_id::text, ft.slug AS focused_tenant_slug, "
        "c.created_by_user_id::text, c.investigation_id::text, c.title, "
        "c.model_name, c.status, c.total_tokens, c.total_dollars, "
        "c.budget_dollars, c.created_at, c.last_message_at "
        "FROM conversations c "
        "LEFT JOIN tenants ft ON ft.id = c.focused_tenant_id "
        "WHERE " + " AND ".join("c." + cond if cond != "1=1" else cond for cond in conds)
        + " ORDER BY c.created_at DESC LIMIT :n OFFSET :off"
    )
    async with _chat_db(request, identity) as db:
        rows = (await db.execute(text(sql), params)).mappings().all()
    return _ConversationListOut(items=[_ConversationOut(**dict(r)) for r in rows])


@router.get(
    "/conversations/{conv_id}",
    response_model=_ConversationDetailOut,
    dependencies=[Depends(_CHAT_GUARD)],
)
async def get_conversation(conv_id: str, request: Request) -> _ConversationDetailOut:
    identity = current_identity(request)
    async with _chat_db(request, identity) as db:
        conv = (
            await db.execute(
                text(
                    """
                    SELECT c.id::text, c.tenant_id::text, c.scope,
                           c.focused_tenant_id::text,
                           ft.slug AS focused_tenant_slug,
                           c.created_by_user_id::text,
                           c.investigation_id::text, c.title, c.model_name,
                           c.status, c.total_tokens, c.total_dollars,
                           c.budget_dollars, c.created_at, c.last_message_at
                    FROM conversations c
                    LEFT JOIN tenants ft ON ft.id = c.focused_tenant_id
                    WHERE c.id = :id
                    """
                ),
                {"id": conv_id},
            )
        ).mappings().first()
        if conv is None:
            raise HTTPException(404, "conversation not found")
        msgs = (
            await db.execute(
                text(
                    """
                    SELECT id::text, role, content, tokens_in, tokens_out,
                           dollars, created_at
                    FROM chat_messages
                    WHERE conversation_id = :cid
                    ORDER BY created_at ASC, id ASC
                    LIMIT 200
                    """
                ),
                {"cid": conv_id},
            )
        ).mappings().all()
    return _ConversationDetailOut(
        conversation=_ConversationOut(**dict(conv)),
        messages=[_MessageOut(**dict(m)) for m in msgs],
    )


@router.delete("/conversations/{conv_id}", dependencies=[Depends(_CHAT_GUARD)])
async def delete_conversation(conv_id: str, request: Request) -> dict[str, bool]:
    identity = current_identity(request)
    async with _chat_db(request, identity) as db:
        await db.execute(
            text("UPDATE conversations SET status = 'closed' WHERE id = :id"),
            {"id": conv_id},
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Streaming turn endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/conversations/{conv_id}/messages",
    dependencies=[Depends(_CHAT_GUARD)],
)
async def post_message(
    conv_id: str, body: _PostMessageBody, request: Request
) -> StreamingResponse:
    """Append a user message and stream the assistant's reply.

    Session lifecycle is subtle:

    * Pre-stream reads/writes (lock the conversation row, daily cap
      check, persist the user message, pre-load investigation summary)
      happen on the **request-bound session**. The middleware commits
      this session when the route returns 200, which releases the FOR
      UPDATE lock — fine, the lock's job is done by then.
    * The streaming agent loop runs on a **fresh session** opened
      inside the stream generator. Reusing the request session here
      blew up with ``asyncpg.InterfaceError: another operation in
      progress`` because the middleware's commit fires the moment the
      route returns the StreamingResponse, while the agent is still
      mid-query.

    Role-aware: MSSP-level callers get a BYPASSRLS session for the
    stream (so the agent's read tools can answer cross-tenant
    questions); tenant-level callers get an app session with
    ``tenant_context`` pinned to their home tenant.
    """
    identity = current_identity(request)

    try:
        conv_uuid = UUID(conv_id)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "invalid conv_id") from e

    # Prelude (lock + cap check + user message insert + investigation
    # summary load) runs in an audience-appropriate session — MSSP gets
    # a fresh BYPASSRLS session so fleet convs (tenant_id IS NULL)
    # write through under an MSSP user's Open-SOC-pinned request.
    async with _chat_db(request, identity) as db:
        # FOR UPDATE lock prevents two browser tabs from triggering
        # concurrent turns on the same conversation and double-billing.
        # We pull focused_tenant_id + the joined slug here too so the
        # turn context can default fleet tool calls to it.
        conv = (
            await db.execute(
                text(
                    """
                    SELECT c.id::text, c.tenant_id::text, c.scope,
                           c.focused_tenant_id::text,
                           ft.slug AS focused_tenant_slug,
                           c.created_by_user_id::text,
                           c.investigation_id::text, c.model_name,
                           c.status, c.total_dollars, c.dollars_unpriced,
                           c.budget_dollars, c.title
                    FROM conversations c
                    LEFT JOIN tenants ft ON ft.id = c.focused_tenant_id
                    WHERE c.id = :id FOR UPDATE OF c NOWAIT
                    """
                ),
                {"id": conv_id},
            )
        ).mappings().first()
        if conv is None:
            raise HTTPException(404, "conversation not found")
        # Ownership gate for MSSP callers (Codex): the BYPASSRLS session sees
        # every conversation, and list_conversations already scopes MSSP users to
        # their own creations — post_message must mirror that, else an MSSP user
        # with another conversation's UUID could drive a turn on it and spend
        # that row's tenant's LLM budget/key. Tenant-audience callers stay
        # RLS-scoped (the row wouldn't be returned for another tenant). 404 (not
        # 403) so a non-owner can't probe conversation existence.
        if (
            identity.role in MSSP_LEVEL_ROLES
            and conv["created_by_user_id"] != str(identity.user_id)
        ):
            raise HTTPException(404, "conversation not found")
        if conv["status"] != "active":
            raise HTTPException(
                409, f"conversation is {conv['status']}"
            )

        scope = conv["scope"]
        tenant_id: UUID | None = (
            UUID(conv["tenant_id"]) if conv["tenant_id"] else None
        )

        # Daily-cap check: tenant cap for tenant-scope convs (shared
        # with worker), per-MSSP-user cap for fleet convs (parallel
        # ceiling so fleet isn't a budget side-door).
        if scope == "tenant":
            assert tenant_id is not None  # invariant from ck_conversations_scope
            if (
                await assert_tenant_daily_cap_ok(db, tenant_id, source="chat_post")
                is None
            ):
                raise HTTPException(429, "tenant daily cap exceeded")
        else:  # mssp_fleet
            if (
                await assert_mssp_user_daily_cap_ok(
                    db, identity.user_id, source="chat_post_fleet"
                )
                is None
            ):
                raise HTTPException(429, "MSSP user daily cap exceeded")

        # Append the user message + auto-title on first user message.
        # tenant_id mirrors the conversation's scope: NULL for fleet,
        # set for tenant.
        await db.execute(
            text(
                """
                INSERT INTO chat_messages (
                    conversation_id, tenant_id, role, content, created_at
                )
                VALUES (:cid, :t, 'user', CAST(:content AS jsonb), now())
                """
            ),
            {
                "cid": conv_id,
                "t": str(tenant_id) if tenant_id else None,
                "content": '{"text": ' + _json_str(body.text) + "}",
            },
        )
        if not conv["title"]:
            await db.execute(
                text("UPDATE conversations SET title = :t WHERE id = :id"),
                {"t": _title_from_text(body.text), "id": conv_id},
            )

        # Pre-load investigation summary if scoped.
        summary = None
        if conv["investigation_id"]:
            try:
                summary = await load_investigation_summary(
                    db, UUID(conv["investigation_id"])
                )
            except Exception:  # noqa: BLE001
                summary = None
    # Prelude session has committed (MSSP) / will be committed by
    # middleware (tenant). Stream session below opens fresh.

    # Capture pre-stream context (the agent only needs values, not the
    # request session itself — by the time _stream() runs the
    # middleware has already committed and the session is closing).
    from soctalk.core.ir.policies import resolve_cost_tracking

    # Tenant-scoped conversations honour the tenant's switch; a fleet
    # conversation spans tenants, so it keeps accounting on.
    cost_tracking = True
    _ct_tenant = conv.get("tenant_id")
    if _ct_tenant:
        try:
            cost_tracking = await resolve_cost_tracking(db, UUID(str(_ct_tenant)))
        except Exception:  # noqa: BLE001 - a policy read must not break chat
            cost_tracking = True

    ctx = TurnContext(
        conversation_id=conv_uuid,
        tenant_id=tenant_id,
        scope=scope,
        focused_tenant_id=(
            UUID(conv["focused_tenant_id"])
            if conv.get("focused_tenant_id")
            else None
        ),
        focused_tenant_slug=conv.get("focused_tenant_slug"),
        user_id=identity.user_id,
        model_name=conv["model_name"],
        budget_dollars=float(conv["budget_dollars"]),
        total_dollars=float(conv["total_dollars"]),
        total_dollars_unpriced=float(conv.get("dollars_unpriced") or 0.0),
        cost_tracking=cost_tracking,
        investigation_id=(
            UUID(conv["investigation_id"]) if conv["investigation_id"] else None
        ),
        investigation_summary=summary,
        user_text=body.text,
    )

    is_mssp = identity.role in MSSP_LEVEL_ROLES

    async def _stream() -> Any:
        from soctalk.core.tenancy.context import tenant_context
        from soctalk.core.tenancy.db import (
            get_app_sessionmaker,
            get_mssp_sessionmaker,
        )

        # Fresh session for the agent loop — the request-bound session
        # has been (or is about to be) committed by the middleware on
        # the 200 response. Reusing it triggers asyncpg's "another
        # operation in progress" InterfaceError mid-stream.
        #
        # Session choice keys on the CALLER'S role (MSSP audience reads
        # cross-tenant for the chat's read tools; tenant-role caller
        # gets RLS-pinned). The conv's scope decides what rows the loop
        # writes (NULL tenant_id for fleet), which is orthogonal to the
        # session role — MSSP-BYPASS handles NULL writes fine, tenant
        # writes still match the RLS predicate.
        if is_mssp:
            sm = get_mssp_sessionmaker()
            tctx = None
        else:
            sm = get_app_sessionmaker()
            tctx = identity.tenant_id

        try:
            async with sm() as stream_db:
                if tctx is not None:
                    async with tenant_context(stream_db, tctx):
                        async for frame in _run_with_disconnect(
                            request, ctx, stream_db
                        ):
                            yield frame
                        await stream_db.commit()
                else:
                    async for frame in _run_with_disconnect(
                        request, ctx, stream_db
                    ):
                        yield frame
                    await stream_db.commit()
        except asyncio.CancelledError:
            ctx.disconnected.set()
            raise

    return StreamingResponse(_stream(), media_type="text/event-stream")


async def _run_with_disconnect(
    request: Request, ctx: TurnContext, db: AsyncSession
) -> Any:
    """Wrap ``run_turn`` with the disconnect-poller task lifecycle."""
    disco_task = asyncio.create_task(_poll_disconnect(request, ctx))
    try:
        async for frame in run_turn(db, ctx):
            yield frame
    finally:
        ctx.disconnected.set()
        disco_task.cancel()


async def _poll_disconnect(request: Request, ctx: TurnContext) -> None:
    while not ctx.disconnected.is_set():
        if await request.is_disconnected():
            ctx.disconnected.set()
            return
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Stop endpoint (best-effort signal; the disconnect poller will pick it up)
# ---------------------------------------------------------------------------


@router.post("/conversations/{conv_id}/stop", dependencies=[Depends(_CHAT_GUARD)])
async def stop_conversation(conv_id: str, request: Request) -> dict[str, bool]:
    # Phase 1: we don't keep a side-channel registry of in-flight
    # turns. The /messages stream cancels naturally when the user
    # closes the request (which the disconnect poller catches). The
    # endpoint exists for future use; ack-only for now.
    return {"ok": True}


# ---------------------------------------------------------------------------
# Confirm proposed action
# ---------------------------------------------------------------------------


@router.post(
    "/conversations/{conv_id}/messages/{msg_id}/confirm",
    dependencies=[Depends(_CHAT_CONFIRM_GUARD)],
)
async def confirm_action(
    conv_id: str, msg_id: str, request: Request
) -> dict[str, Any]:
    identity = current_identity(request)
    async with _chat_db(request, identity) as db:
        try:
            result = await chat_actions.dispatch_confirm(
                db,
                message_id=UUID(msg_id),
                conversation_id=UUID(conv_id),
                reviewer_user_id=identity.user_id,
                reviewer_email=identity.email,
            )
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
    return result


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _json_str(s: str) -> str:
    """JSON-encode a string (with quotes) for inline SQL literal."""
    import json as _j

    return _j.dumps(s)
