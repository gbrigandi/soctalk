"""Chat-turn execution loop.

Public entry: :func:`run_turn` — async generator that yields SSE bytes
as the assistant works. The caller (the chat POST handler) consumes
the generator, writes each frame to the StreamingResponse, and polls
``request.is_disconnected()`` between frames so a closed tab cancels
the LLM call promptly.

Loop shape:

1. Load the conversation history (system + recent turns + tool results).
2. Apply context eviction (system + last 2 user/assistant turns kept
   full; older tool results replaced with 1-line summaries; oldest
   dropped first if still over the per-turn cap).
3. Bind the read-only tools (`AVAILABLE_TOOLS`) to the model.
4. Invoke the model with the message list.
5. For each model response:
   - If text: stream as ``delta`` events; accumulate into the assistant
     message.
   - If tool call: emit ``tool_call``; dispatch; emit ``tool_result``;
     append to history; re-invoke.
   - If a ``propose_action`` tool call (schema-enforced structured
     output, issue #10): validate + emit ``proposed_action``, persist
     as a ``role='action'`` chat_messages row.
6. After end-of-turn, emit ``usage`` and ``done``.

Mid-stream concerns:

* ``request.is_disconnected()`` polling — the caller decides; this loop
  surfaces a ``cancelled=True`` flag via the ``Cancelled`` sentinel.
* Per-conversation budget exhaustion — checked at the top of every
  model invocation; on overshoot the loop emits ``budget_exhausted``
  + ``done`` (stop_reason=budget_exhausted) and persists what it has.
* Provider errors — classified via the verdict node's
  ``_classify_llm_error`` helper so credit-lack / rate-limit don't leak
  raw messages.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import structlog
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.chat import actions, prompts, sse
from soctalk.chat.mcp_tools import build_mcp_chat_tools
from soctalk.chat.tools import (
    AVAILABLE_TOOLS,
    ChatTool,
    TenantSlugMismatch,
    TenantSlugRequired,
    TenantSlugUnknown,
    ToolResult,
    get_investigation,
    resolve_tenant_slug,
)


def _active_tools(scope: str) -> list[ChatTool]:
    """Read-only DB tools + native Wazuh primitives + MCP-bound tools.

    Fleet-only tools (``ChatTool.fleet_only``) are excluded in
    tenant-scope conversations so a tenant-bound MSSP chat can't peek
    at the fleet without explicitly switching scope.
    """
    from soctalk.chat.wazuh_primitives import WAZUH_CHAT_TOOLS

    pool = [*AVAILABLE_TOOLS, *WAZUH_CHAT_TOOLS, *build_mcp_chat_tools()]
    if scope == "mssp_fleet":
        return pool
    # tenant scope: drop fleet-only tools.
    return [t for t in pool if not t.fleet_only]


def _find_tool(name: str, pool: list[ChatTool]) -> ChatTool | None:
    for t in pool:
        if t.name == name:
            return t
    return None
from soctalk.config import get_config
from soctalk.core.pricing.resolve import resolve_run_prices
from soctalk.graph import budget as token_budget
from soctalk.inference import InferenceTier, resolve_tier
from soctalk.llm import create_chat_model
from soctalk.supervisor.verdict import _classify_llm_error

logger = structlog.get_logger()


# Per-turn structural limits. The model's own ``max_tokens`` (2k by
# default) bounds the output; this bounds how much *prior* context we
# stuff back in.
DEFAULT_KEEP_FULL_TURNS = 2  # last N user+assistant turn pairs
MAX_TOOL_ITERATIONS = 6  # safety net for runaway tool loops


# The model proposes a review action by CALLING this tool (issue #10) — a
# schema-enforced structured output, the same mechanism triage uses, replacing
# the old free-text ``<action>{json}</action>`` block the loop regex-parsed. The
# tool never executes anything: it surfaces a confirm button; ``actions.
# dispatch_confirm`` runs the call server-side only when the analyst confirms.
_PROPOSE_ACTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_action",
        "description": (
            "Propose a review action for the analyst to confirm. This surfaces a "
            "confirmation button in the UI; it does NOT execute until the analyst "
            "clicks Confirm. Use the exact target.id returned by a prior tool call — "
            "never invent IDs. Never include URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(actions.ALLOWED_ACTIONS),
                    "description": "The review action verb.",
                },
                "target": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["pending_review", "investigation"]},
                        "id": {"type": "string", "description": "UUID from a tool result."},
                        "title": {"type": "string"},
                    },
                    "required": ["kind", "id"],
                },
                "reason": {"type": "string", "description": "One-sentence justification."},
                "evidence": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Supporting facts drawn from tool results.",
                },
                "confidence": {"type": "number", "description": "Confidence in [0,1]."},
                "feedback": {"type": "string", "description": "Optional note stored with the decision."},
            },
            "required": ["action", "target", "reason"],
        },
    },
}


def _handle_propose_action(targs: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Map a ``propose_action`` tool call to a proposed-action payload.

    Returns ``(payload, ack)`` — ``payload`` is None when the args fail
    validation, and ``ack`` is the ToolMessage content fed back to the model so
    it learns the outcome (surfaced, or rejected-with-reason so it can retry).
    Validation lives in ``actions.build_proposed_action`` (unchanged) — the
    model is never trusted to point at endpoints.
    """
    # ``target`` may arrive as a non-dict (the model is untrusted) — coerce so
    # ``.get`` can't raise AttributeError out of the try and abort the turn
    # before the ToolMessage ack is appended.
    target = targs.get("target")
    if not isinstance(target, dict):
        target = {}
    try:
        payload = actions.build_proposed_action(
            action=targs.get("action"),
            target_kind=target.get("kind"),
            target_id=target.get("id"),
            target_title=target.get("title"),
            reason=targs.get("reason") or "",
            evidence=targs.get("evidence") or [],
            confidence=targs.get("confidence"),
            feedback=targs.get("feedback"),
        )
        return payload, "Action surfaced to the analyst for confirmation."
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        logger.warning("chat_propose_action_invalid err=%s", str(e))
        return None, (
            f"propose_action rejected: {e}. Fix the arguments and call it again, "
            "or continue without proposing an action."
        )


@dataclass(slots=True)
class TurnContext:
    """Everything the loop needs to know about one turn."""

    conversation_id: UUID
    # NULL for scope='mssp_fleet' — the agent operates without a single
    # tenant binding and tools require explicit tenant_slug.
    tenant_id: UUID | None
    # 'tenant' | 'mssp_fleet' — drives system prompt + tool requirements.
    scope: str
    user_id: UUID
    model_name: str
    budget_dollars: float
    total_dollars: float
    investigation_id: UUID | None
    # Pre-fetched compact investigation context (or None).
    investigation_summary: dict[str, Any] | None
    # The user's new message text (already persisted as a row before
    # ``run_turn`` is invoked).
    user_text: str
    # Soft fleet-scope focus (settable via ``set_fleet_focus`` tool).
    # When set, fleet tool calls that omit ``tenant_slug`` default to
    # this tenant instead of erroring. Updated in-place by the tool
    # for the rest of the current turn (subsequent calls within the
    # same turn pick it up). Persisted to ``conversations.focused_
    # tenant_id`` so future turns inherit it.
    focused_tenant_id: UUID | None = None
    focused_tenant_slug: str | None = None
    # Set by the caller to signal cancellation (closed tab, /stop).
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    # Of ``total_dollars``, the portion priced by guesswork. Subtracted before
    # any budget comparison; the total itself stays whole so the recorded bill
    # shows what was actually spent (#124). Defaulted, so it sits with the
    # other optional fields — a defaulted field cannot precede required ones.
    total_dollars_unpriced: float = 0.0
    # False when the tenant has dollar accounting off. Both conversation
    # budget gates are skipped then, matching the per-run and daily behaviour
    # and the panel's promise (Codex round 6).
    cost_tracking: bool = True


# ---------------------------------------------------------------------------
# History loading + eviction
# ---------------------------------------------------------------------------


async def _load_history(
    db: AsyncSession, conversation_id: UUID
) -> list[dict[str, Any]]:
    """Pull the conversation log in insertion order.

    JSONB columns come back from asyncpg as *str* when accessed via
    raw ``text()`` SQL (no SQLAlchemy column type info). Deserialise
    explicitly so downstream ``content.get('text')`` works — without
    this the agent silently dropped every user message and Anthropic
    rejected the turn with ``messages: at least one message is required``.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT id::text, role, content, created_at
                FROM chat_messages
                WHERE conversation_id = :cid
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"cid": str(conversation_id)},
        )
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        c = d.get("content")
        if isinstance(c, str):
            try:
                d["content"] = json.loads(c)
            except (TypeError, ValueError):
                d["content"] = {}
        out.append(d)
    return out
    # Of ``total_dollars``, the portion priced by guesswork. Subtracted
    # before any budget comparison; the total itself stays whole so the
    # recorded bill shows what was actually spent (#124).
    total_dollars_unpriced: float = 0.0

def _summarise_tool_row(row: dict[str, Any]) -> str:
    """One-line evictable summary of an old tool call."""
    content = row.get("content") or {}
    name = content.get("name") or "tool"
    args = content.get("args") or {}
    args_str = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    result = content.get("result")
    if isinstance(result, dict) and "data" in result:
        data = result["data"]
    else:
        data = result
    if isinstance(data, list):
        summary = f"{len(data)} rows"
    elif isinstance(data, dict):
        keys = list(data.keys())[:3]
        summary = "keys=" + ",".join(keys)
    else:
        summary = str(data)[:60]
    return f"[older] {name}({args_str}) → {summary}"


def _build_messages(
    history: list[dict[str, Any]],
    *,
    system_prompt: str,
    keep_full_turns: int = DEFAULT_KEEP_FULL_TURNS,
) -> list[BaseMessage]:
    """Apply context eviction and return the LangChain message list.

    Strategy:
    * Always include the system prompt.
    * Walk backwards from the end of history; keep full content for
      the last ``keep_full_turns`` user+assistant pairs (plus any
      adjacent tool messages from those turns).
    * Older user/assistant messages: keep full text (cheap).
    * Older tool messages: replace with a 1-line summary.

    Returns the message list in chronological order (oldest first).
    """
    out: list[BaseMessage] = [SystemMessage(content=system_prompt)]

    # Count back the last N user messages — index of the cutoff.
    user_indices = [i for i, r in enumerate(history) if r["role"] == "user"]
    cutoff_user_idx = (
        user_indices[-keep_full_turns] if len(user_indices) >= keep_full_turns
        else (user_indices[0] if user_indices else 0)
    )

    for i, row in enumerate(history):
        role = row["role"]
        content = row.get("content") or {}
        keep_full = i >= cutoff_user_idx
        if role == "user":
            out.append(HumanMessage(content=content.get("text") or ""))
        elif role == "assistant":
            text_val = content.get("text") or ""
            if keep_full:
                out.append(AIMessage(content=text_val))
            else:
                # Older assistant — still keep, but trimmed.
                out.append(AIMessage(content=text_val[:400]))
        elif role == "tool":
            # Drop tool rows from the history-replay entirely. Reasons:
            # 1. Anthropic forbids ``tool_result`` blocks that aren't
            #    immediately preceded by the matching ``tool_use``
            #    block from the assistant — we don't persist tool_use
            #    intents in chat_messages, so we can't reconstruct the
            #    chain.
            # 2. ``SystemMessage`` workaround for the summary is itself
            #    rejected by langchain-anthropic with
            #    ``ValueError: Received multiple non-consecutive system
            #    messages`` — only ONE system message is allowed and
            #    it must lead.
            # The assistant's text reply for the prior turn *already*
            # incorporates the tool result; dropping the raw tool row
            # loses nothing the model can't recover via a fresh call.
            continue
        elif role == "system":
            # In-line system note (rare; e.g. budget warning). Preserve.
            out.append(SystemMessage(content=content.get("text") or ""))
        elif role == "action":
            # Past action proposal — describe it but don't ask the
            # model to re-emit. Helps the analyst's mental state and
            # the model's continuity without producing duplicate
            # buttons in the UI.
            label = content.get("action", "action")
            tid = (content.get("target") or {}).get("id", "?")
            status = "confirmed" if content.get("confirmed_at") else "pending"
            # Continuity note as an AIMessage — NOT a mid-history SystemMessage:
            # langchain-anthropic rejects non-leading system messages ("multiple
            # non-consecutive system messages"), which would break every turn
            # after the first proposed action (same reason tool rows are dropped).
            out.append(
                AIMessage(content=f"[prior_action] {label} on {tid} ({status})")
            )

    return out


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _dispatch_tool(
    tool: ChatTool,
    db: AsyncSession,
    raw_args: dict[str, Any],
    ctx: TurnContext,
) -> ToolResult:
    """Call the tool function with kwarg unpacking. Catches errors.

    For tenant-targeted tools, resolves ``tenant_slug`` (per scope
    rules) into a ``target_tenant_id`` kwarg and wraps the result with
    a ``_tenant`` envelope when the conversation is fleet-scope (so the
    model can attribute multi-tenant answers correctly).
    """
    args = dict(raw_args)
    slug_in = args.pop("tenant_slug", None)
    target_meta: dict[str, str] | None = None

    if tool.tenant_targeted:
        try:
            target_tid, target_meta = await _resolve_tool_target_tenant(
                db, ctx, slug_in
            )
        except (TenantSlugRequired, TenantSlugMismatch, TenantSlugUnknown) as e:
            return ToolResult(data={"error": str(e)})
        if target_tid is not None:
            args["target_tenant_id"] = target_tid

    # ``set_fleet_focus`` is the one tool that needs handles to the
    # live conversation row + the in-memory ctx so it can persist the
    # focus AND mutate ctx for the rest of the same turn. The leading
    # underscore on the kwarg keeps them out of the JSON Schema the
    # model sees.
    if tool.name == "set_fleet_focus":
        args["_conversation_id"] = ctx.conversation_id
        args["_ctx"] = ctx

    try:
        result = await tool.func(db, **args)  # type: ignore[arg-type]
    except TypeError as e:
        return ToolResult(data={"error": f"bad arguments: {e}"})
    except Exception as e:  # noqa: BLE001
        logger.exception("chat_tool_error tool=%s", tool.name)
        return ToolResult(data={"error": f"{type(e).__name__}: {e}"[:200]})

    # Only add the _tenant envelope in fleet scope. Tenant scope keeps
    # the existing flat shape (the tenant is implicit and uniform across
    # results, so the envelope would be pure noise).
    if target_meta is not None and ctx.scope == "mssp_fleet":
        result = _wrap_result_with_tenant(result, target_meta)
    return result


async def _resolve_tool_target_tenant(
    db: AsyncSession,
    ctx: TurnContext,
    slug_in: str | None,
) -> tuple[UUID | None, dict[str, str] | None]:
    """Translate ``tenant_slug`` arg + ctx scope to ``(tenant_id, meta)``.

    * tenant scope, slug omitted        → (ctx.tenant_id, None)
    * tenant scope, slug matches conv   → (ctx.tenant_id, None)
    * tenant scope, slug mismatches     → TenantSlugMismatch
    * fleet  scope, slug omitted        → TenantSlugRequired
    * fleet  scope, slug given          → resolve to (tid, meta)
    """
    if ctx.scope == "tenant":
        assert ctx.tenant_id is not None  # ck_conversations_scope invariant
        if slug_in is None:
            return (ctx.tenant_id, None)
        # Caller passed a slug — validate it matches the conv's tenant.
        try:
            sid, sslug, sname = await resolve_tenant_slug(db, slug_in)
        except TenantSlugUnknown:
            raise
        if sid != ctx.tenant_id:
            raise TenantSlugMismatch(
                f"tenant_slug {slug_in!r} doesn't match this "
                "conversation's tenant — drop the arg or open a fleet "
                "conversation"
            )
        return (ctx.tenant_id, None)

    # mssp_fleet scope.
    if not slug_in:
        # Fall back to the conversation's focus, if any. The agent sets
        # focus via the ``set_fleet_focus`` tool when the user says
        # things like "let's work on lab tenant".
        if ctx.focused_tenant_id is not None:
            slug = ctx.focused_tenant_slug or ""
            return (
                ctx.focused_tenant_id,
                {
                    "id": str(ctx.focused_tenant_id),
                    "slug": slug,
                    "name": slug,
                },
            )
        raise TenantSlugRequired(
            "tenant_slug required in fleet conversations — call "
            "set_fleet_focus(slug_or_name) once if the user signalled "
            "a tenant to work on, or list_tenants() to find one"
        )
    sid, sslug, sname = await resolve_tenant_slug(db, slug_in)
    return (sid, {"id": str(sid), "slug": sslug, "name": sname})


def _wrap_result_with_tenant(
    result: ToolResult, tenant_meta: dict[str, str]
) -> ToolResult:
    """Add ``_tenant`` to the result's data envelope.

    For list payloads we wrap as ``{"_tenant": ..., "rows": [...]}``;
    dicts get ``_tenant`` merged in at the top level.
    """
    if isinstance(result.data, dict):
        wrapped = {"_tenant": tenant_meta, **result.data}
    else:
        wrapped = {"_tenant": tenant_meta, "rows": result.data}
    return ToolResult(
        data=wrapped,
        truncated=result.truncated,
        hint=result.hint,
    )


# ---------------------------------------------------------------------------
# LLM tool-binding (provider-agnostic via LangChain)
# ---------------------------------------------------------------------------


def _tool_specs_for_binding(pool: list[ChatTool]) -> list[dict[str, Any]]:
    """Render tool schemas in the LangChain ``tool`` format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.schema,
            },
        }
        for t in pool
    ]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def _insert_message(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    tenant_id: UUID | None,
    role: str,
    content: dict[str, Any],
    tokens_in: int = 0,
    tokens_out: int = 0,
    dollars: float = 0.0,
    dollars_unpriced: float = 0.0,
    model_name: str | None = None,
) -> UUID:
    """Insert one chat_messages row and return its id.

    ``tenant_id=None`` is the fleet-scope path: the row matches the
    NULL-tenant RLS branch (audience='mssp' + blank GUC) — MSSP
    sessions handle this via BYPASSRLS.
    """
    msg_id = uuid4()
    await db.execute(
        text(
            """
            INSERT INTO chat_messages (
                id, conversation_id, tenant_id, role, content,
                tokens_in, tokens_out, dollars, dollars_unpriced,
                model_name, created_at
            ) VALUES (
                :id, :cid, :t, :role, CAST(:content AS jsonb),
                :tin, :tout, :dollars, :dollars_unpriced, :model, now()
            )
            """
        ),
        {
            "id": str(msg_id),
            "cid": str(conversation_id),
            "t": str(tenant_id) if tenant_id else None,
            "role": role,
            "content": json.dumps(content, default=str),
            "tin": tokens_in,
            "tout": tokens_out,
            "dollars": dollars,
            "dollars_unpriced": dollars_unpriced,
            "model": model_name,
        },
    )
    return msg_id


async def _update_conversation_totals(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    add_tokens: int,
    add_dollars: float,
    add_dollars_unpriced: float = 0.0,
    new_status: str | None = None,
) -> tuple[int, float]:
    """Increment the rolling totals on the conversation row.

    Returns ``(new_total_tokens, new_total_dollars)`` so the caller
    can include them in the ``usage`` SSE frame without a re-read.
    """
    row = (
        await db.execute(
            text(
                """
                UPDATE conversations
                   SET total_tokens = total_tokens + :tk,
                       total_dollars = total_dollars + :d,
                       dollars_unpriced = dollars_unpriced + :du,
                       last_message_at = now(),
                       status = COALESCE(:st, status)
                 WHERE id = :id
                RETURNING total_tokens, total_dollars
                """
            ),
            {
                "tk": int(add_tokens),
                "d": float(add_dollars),
                "du": float(add_dollars_unpriced),
                "st": new_status,
                "id": str(conversation_id),
            },
        )
    ).mappings().first()
    if row is None:
        return (0, 0.0)
    return (int(row["total_tokens"] or 0), float(row["total_dollars"] or 0.0))


async def _tenant_chat_llm_config(
    db: AsyncSession, tenant_id: UUID, base, requested_model: str
):
    """Overlay a tenant's ``IntegrationConfig`` onto the global chat LLMConfig.

    The chat agent runs in the shared API process, so it can't read per-tenant
    ``SOCTALK_*`` env the way each single-tenant worker does. This loads the
    tenant's stored LLM config and overlays provider / base_url / model / key
    onto the process-global chat config so a BYOK or custom-model tenant's chat
    runs on THEIR backend — matching what its runs-worker uses — instead of the
    MSSP shared install config.

    Returns ``(llm_config, effective_model)``. Chat sampling
    (``chat_temperature`` / ``chat_max_tokens``) stays global — no per-tenant
    column for it (a possible follow-up).

    Guards (Codex review):
    * No ``IntegrationConfig`` row → ``(base, requested_model)`` (global).
    * A tenant WITHOUT its own key relies on the shared install credential, which
      only authenticates against the install's provider. If the tenant's provider
      differs and it has no own key, an overlay would build a keyless client for
      the wrong vendor — fall back to ``base`` instead (the pre-feature
      behaviour). A default/unconfigured row on a matching provider still uses
      the shared key safely.
    * The process-global ``chat`` tier (``SOCTALK_CHAT_*``) is dropped from the
      copied config so ``resolve_tier`` uses the overlaid top-level backend, not
      the shared chat-tier backend/key.
    * When the overlay changes provider, the conversation's stored model was
      chosen for the old/shared provider and won't run on the tenant backend —
      the tenant's own model becomes the effective model. Same provider → the
      per-conversation model choice is respected.
    """
    from sqlalchemy import select

    from soctalk.core.tenancy.models import IntegrationConfig

    integ = (
        await db.execute(
            select(IntegrationConfig).where(IntegrationConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if integ is None:
        return base, requested_model
    provider = (
        "openai"
        if integ.llm_provider in ("openai", "openai-compatible")
        else "anthropic"
    )
    has_own_key = bool(integ.llm_api_key_plain)
    if provider != base.provider and not has_own_key:
        # Can't authenticate a cross-provider tier on the shared install key.
        return base, requested_model
    # resolve_tier(CHAT) resolves the model as ``chat_model or fast_model``; set
    # both to the tenant model so it wins whether or not a chat_model is set.
    model = integ.llm_model or base.chat_model or base.fast_model
    update: dict[str, Any] = {
        "provider": provider,
        "chat_model": model,
        "fast_model": model,
    }
    if base.tiers:
        # Drop the shared 'chat' tier so it can't override the overlaid backend.
        chatless = {k: v for k, v in base.tiers.items() if k != "chat"}
        if chatless != base.tiers:
            update["tiers"] = chatless
    if provider == "openai":
        if integ.llm_base_url:
            update["openai_base_url"] = integ.llm_base_url
        if integ.llm_api_key_plain:
            update["openai_api_key"] = integ.llm_api_key_plain
    else:
        if integ.llm_base_url:
            update["anthropic_base_url"] = integ.llm_base_url
        if integ.llm_api_key_plain:
            update["anthropic_api_key"] = integ.llm_api_key_plain
    cfg = base.model_copy(update=update)
    effective_model = model if provider != base.provider else requested_model
    return cfg, effective_model


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_turn(
    db: AsyncSession, ctx: TurnContext
) -> AsyncIterator[bytes]:
    """Yield SSE frames for one assistant turn.

    The caller is responsible for:
    * Holding the conversation row's ``FOR UPDATE`` lock (taken in
      ``chat.py`` before this is invoked).
    * Calling ``ctx.disconnected.set()`` when the client disconnects.
    """
    # 0. Per-conversation budget check up-front. If already exhausted,
    #    short-circuit before any LLM cost.
    # Enforceable, not raw: an unpriced turn recorded real dollars, and gating
    # the NEXT turn on them would block a conversation over money the same
    # module refuses to enforce mid-turn (Codex round 3, P1).
    if ctx.cost_tracking and (
        ctx.total_dollars - ctx.total_dollars_unpriced >= ctx.budget_dollars
    ):
        yield sse.error(
            "budget_exhausted",
            "This conversation has reached its $%.2f cap." % ctx.budget_dollars,
        )
        yield sse.done(str(uuid4()), stop_reason="budget_exhausted")
        return

    # 1. Build the system prompt.
    if ctx.investigation_summary:
        system_prompt = prompts.per_investigation_system_prompt(
            ctx.investigation_summary
        )
    elif ctx.scope == "mssp_fleet":
        system_prompt = prompts.fleet_system_prompt(
            focused_tenant_slug=ctx.focused_tenant_slug,
        )
    else:
        system_prompt = prompts.GLOBAL_SYSTEM_PROMPT

    # 2. Load history + build the message list with eviction.
    history = await _load_history(db, ctx.conversation_id)
    messages = _build_messages(history, system_prompt=system_prompt)

    # Defensive: ensure the CURRENT user message is in the list. The
    # handler persists ``ctx.user_text`` to chat_messages BEFORE
    # invoking ``run_turn`` and the middleware commits the row well
    # before this stream session's transaction begins — but pgbouncer
    # / asyncpg pool reuse + SQLAlchemy session snapshotting can in
    # rare cases hand us a session whose snapshot pre-dates the
    # commit, returning empty ``history`` and producing
    # ``messages: [SystemMessage]``. Anthropic rejects that with 400
    # ("at least one message is required"). Appending ctx.user_text
    # when history's last user message isn't ours costs us nothing on
    # the happy path and saves the turn from silently failing.
    last_user_text = next(
        (
            (r.get("content") or {}).get("text")
            for r in reversed(history)
            if r["role"] == "user"
        ),
        None,
    )
    if last_user_text != ctx.user_text:
        messages.append(HumanMessage(content=ctx.user_text))

    # 3. Set up the model with tools. Resolve the CHAT tier (issue #10/#4) so
    # chat inherits per-tier provider/base_url/engine + scoped credentials, and
    # its sampling comes from config rather than hardcoded literals. The
    # per-conversation model (ctx.model_name) is preserved as the tier override.
    app_config = get_config()
    # Per-tenant chat (issue #10/#4): a tenant-scoped conversation resolves the
    # CHAT tier from the TENANT's own LLM config (provider/model/key), matching
    # what its worker runs, not the MSSP shared install. Fleet scope
    # (tenant_id None) stays on the process-global config. A cross-provider
    # overlay also swaps the effective model (the stored conversation model was
    # chosen for the old provider) — see _tenant_chat_llm_config.
    llm_cfg = app_config.llm
    model_override = ctx.model_name
    if ctx.tenant_id is not None:
        llm_cfg, model_override = await _tenant_chat_llm_config(
            db, ctx.tenant_id, app_config.llm, ctx.model_name
        )
    resolved = resolve_tier(
        llm_cfg, InferenceTier.CHAT, model_override=model_override
    )
    llm = create_chat_model(
        resolved.llm_config,
        model=resolved.model,
        temperature=app_config.llm.chat_temperature,
        max_tokens=app_config.llm.chat_max_tokens,
    )
    # Snapshot the active toolset for this turn — read-only DB tools
    # plus whatever MCP servers (Wazuh, Cortex, …) are currently bound,
    # plus the propose_action tool the model calls to surface a review action.
    # Fleet roll-ups are filtered out for tenant-scope conversations.
    active_pool = _active_tools(ctx.scope)
    try:
        llm = llm.bind_tools(
            _tool_specs_for_binding(active_pool) + [_PROPOSE_ACTION_TOOL]
        )
    except (AttributeError, TypeError):
        # Some providers don't support bind_tools; the loop still
        # works as a chat-only agent without tool calls.
        logger.warning("chat_bind_tools_unavailable model=%s", ctx.model_name)

    # 4. Iterate: invoke → handle response (text/tool_call) → repeat.
    assistant_text_buf = ""
    final_message_id = uuid4()
    total_tokens_in = 0
    total_tokens_out = 0
    total_turn_dollars = 0.0
    # What the budget check may act on: total minus spend priced by guesswork
    # (#124). Kept apart from total_turn_dollars, which is what gets recorded.
    total_turn_enforceable = 0.0
    iterations = 0
    stop_reason = "end_turn"
    # Hoisted above the loop: the model calls propose_action on one iteration
    # and finishes on a later one, so this must survive across iterations to be
    # persisted after the turn. Last proposal wins.
    action_payload: dict[str, Any] | None = None

    # Chat has no run row to carry a price snapshot, so it resolves one for
    # itself, once per turn, from the same catalog and tenant override a run
    # would use (#125). Without this, retiring the env overlay would leave chat
    # priced only by the built-in table, which is exactly the blind spot the
    # catalog exists to close. Never fatal: a pricing problem must not stop the
    # conversation, it just falls back to the shipped defaults.
    chat_prices: dict[str, Any] | None = None
    _price_tenant = ctx.focused_tenant_id or ctx.tenant_id
    if _price_tenant is not None:
        try:
            # Price the model this CONVERSATION runs, not the tenant default.
            # A conversation can be created with an explicit model and keeps it
            # as a tier override, so pricing the default stamped a model the
            # turn never calls — and the turn's spend then fell through to the
            # built-in table or to `unknown` (Codex, round 4).
            # `resolved.model`, not ctx.model_name. resolve_tier applies the
            # tenant overlay and can swap the effective model for a
            # cross-provider tenant, so ctx.model_name is what was REQUESTED
            # and resolved.model is what create_chat_model was actually handed
            # (Codex, round 5). Pricing the requested one would stamp a model
            # the turn never calls, which is the bug this parameter exists to
            # fix.
            chat_prices = await resolve_run_prices(
                db, _price_tenant, chat_model=resolved.model
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat_price_resolution_failed error=%s", exc)

    try:
        while iterations < MAX_TOOL_ITERATIONS:
            iterations += 1
            if ctx.disconnected.is_set():
                stop_reason = "disconnected"
                break

            response = await llm.ainvoke(messages)
            # Track usage on the in-flight state for budget checks.
            state: dict[str, Any] = {
                "tokens_used": 0,
                "dollars_used": 0.0,
                "tokens_budget": 10**9,
                "dollars_budget": ctx.budget_dollars,
                **({"price_snapshot": chat_prices} if chat_prices else {}),
            }
            token_budget.track(state, response)
            call_in, call_out = token_budget.extract_usage(response)
            total_tokens_in += call_in
            total_tokens_out += call_out
            # Only spend we can attribute counts toward the turn's dollars
            # (#124, #141 phase 2). Chat shares graph/budget.track, so it hit
            # the same fallback: a chat turn on an unpriced model billed at
            # $15/$75 and that invented figure fed the tenant's daily ceiling,
            # which gates the worker's claim loop too — so an unpriced chat
            # session could stop triage (Codex review, phases 1-2, P1).
            #
            # Two accumulators, because they answer different questions. The
            # message row and the conversation total must show what was ACTUALLY
            # spent — an operator reading a bill wants the money, not the
            # enforceable subset — while the budget check may only act on spend
            # it can attribute. Collapsing them into one figure understated the
            # recorded cost, which my first attempt did (Codex round 2, P1).
            total_turn_dollars += float(state["dollars_used"])
            total_turn_enforceable += token_budget.enforceable_dollars(state)
            turn_dollars_this_call = float(state["dollars_used"])

            # Anthropic-via-LangChain returns ``response.content`` as
            # *either* a plain string (text-only turn) or a list of
            # content blocks (text + tool_use mixed). Flatten to a
            # single string so the delta stream works uniformly.
            raw_content = response.content
            if isinstance(raw_content, list):
                response_text = "".join(
                    b.get("text", "") if isinstance(b, dict) and b.get("type") == "text"
                    else (b if isinstance(b, str) else "")
                    for b in raw_content
                )
            else:
                response_text = raw_content or ""
            tool_calls = getattr(response, "tool_calls", None) or []

            # Update message list with the model's response for the
            # next iteration even if tool calls.
            messages.append(response)

            # Stream the text content. (No real token streaming since
            # langchain-anthropic's ainvoke returns the full message;
            # we surface it as one delta. For real streaming we'd use
            # astream — Phase 1 ships ainvoke for simplicity.) Action
            # proposals no longer live in the prose — they arrive as
            # propose_action tool calls (below), so the text is streamed
            # verbatim with no block-stripping.
            display_text = response_text.strip()
            if display_text:
                assistant_text_buf += display_text
                yield sse.delta(display_text)

            if not tool_calls:
                # Model done with tools; exit the loop after streaming.
                break

            for tc in tool_calls:
                if ctx.disconnected.is_set():
                    stop_reason = "disconnected"
                    break
                tname = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                targs = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None) or {}
                call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                call_id = call_id or str(uuid4())

                # propose_action is a structured proposal, not a data tool:
                # validate + surface the confirm button, ack back to the model,
                # and skip the normal dispatch/persist path.
                if tname == "propose_action":
                    payload, ack = _handle_propose_action(targs or {})
                    if payload is not None:
                        action_payload = payload
                        yield sse.proposed_action(payload)
                    messages.append(ToolMessage(content=ack, tool_call_id=call_id))
                    continue

                tool = _find_tool(tname or "", active_pool)
                yield sse.tool_call(call_id, tname or "", targs or {})

                if tool is None:
                    result = ToolResult(data={"error": f"unknown tool: {tname!r}"})
                else:
                    result = await _dispatch_tool(tool, db, targs or {}, ctx)

                yield sse.tool_result(
                    call_id, result.to_dict(), truncated=result.truncated
                )

                # Persist the tool message in chat_messages for audit
                # and history reconstruction on later turns.
                await _insert_message(
                    db,
                    conversation_id=ctx.conversation_id,
                    tenant_id=ctx.tenant_id,
                    role="tool",
                    content={
                        "name": tname,
                        "args": targs or {},
                        "result": result.to_dict(),
                        "call_id": call_id,
                    },
                )

                # Append a ToolMessage so the next ainvoke sees the
                # result in the model's context.
                messages.append(
                    ToolMessage(
                        content=json.dumps(result.to_dict(), default=str),
                        tool_call_id=call_id,
                    )
                )

            if ctx.disconnected.is_set():
                stop_reason = "disconnected"
                break

            # Budget check between iterations.
            # Subtract the conversation's prior unpriced spend here too. The
            # PRE-turn gate already did, but this between-iteration check did
            # not, so a conversation that had recorded unpriced dollars passed
            # the door and was then stopped by the first tool-call iteration
            # (Codex round 4, P1).
            prior_enforceable = ctx.total_dollars - ctx.total_dollars_unpriced
            if ctx.cost_tracking and (
                prior_enforceable + total_turn_enforceable >= ctx.budget_dollars
            ):
                stop_reason = "budget_exhausted"
                break

        # Persist the assistant message.
        if assistant_text_buf or stop_reason != "end_turn":
            await _insert_message(
                db,
                conversation_id=ctx.conversation_id,
                tenant_id=ctx.tenant_id,
                role="assistant",
                content={"text": assistant_text_buf or ""},
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
                dollars=total_turn_dollars,
                dollars_unpriced=total_turn_dollars - total_turn_enforceable,
                model_name=ctx.model_name,
            )

        # Persist the proposed_action if any (last one wins). With the native
        # propose_action tool the payload is fully built when surfaced (no
        # half-built stream), so persist it whenever it was emitted — EXCEPT on
        # ``disconnected`` (the client is gone, no button to back). Notably a
        # ``budget_exhausted`` stop must still persist: the confirm button was
        # already streamed, so a missing row would leave it unclickable.
        if action_payload is not None and stop_reason != "disconnected":
            await _insert_message(
                db,
                conversation_id=ctx.conversation_id,
                tenant_id=ctx.tenant_id,
                role="action",
                content=action_payload,
            )

        # Roll up conversation totals.
        new_total_tokens, new_total_dollars = await _update_conversation_totals(
            db,
            conversation_id=ctx.conversation_id,
            add_tokens=(total_tokens_in + total_tokens_out),
            add_dollars=total_turn_dollars,
            add_dollars_unpriced=total_turn_dollars - total_turn_enforceable,
            new_status=(
                "budget_exhausted" if stop_reason == "budget_exhausted" else None
            ),
        )

        yield sse.usage(
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            dollars=total_turn_dollars,
            conv_total_dollars=new_total_dollars,
        )
        yield sse.done(str(final_message_id), stop_reason=stop_reason)
    except Exception as e:  # noqa: BLE001
        category = _classify_llm_error(e)
        logger.exception(
            "chat_turn_failed conv=%s category=%s",
            str(ctx.conversation_id),
            category,
        )
        yield sse.error(category, "LLM turn failed.")
        yield sse.done(str(final_message_id), stop_reason="error")


# ---------------------------------------------------------------------------
# Pre-turn investigation summary fetcher (used by the API handler to
# pre-load context before invoking the loop).
# ---------------------------------------------------------------------------


async def load_investigation_summary(
    db: AsyncSession, investigation_id: UUID
) -> dict[str, Any] | None:
    """Fetch a compact case summary for the system prompt."""
    res = await get_investigation(db, investigation_id=str(investigation_id))
    if not isinstance(res.data, dict) or res.data.get("error"):
        return None
    return res.data
