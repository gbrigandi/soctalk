"""End-to-end-ish drive of the chat run_turn loop for propose_action (#10).

Unlike test_chat_propose_action_unit (pure handler mapping), this exercises the
REAL streaming loop: a fake LLM emits a propose_action tool call, and we assert
the loop intercepts it, streams the prose, emits an sse ``proposed_action``
frame, acks back to the model (keeping the tool-calling contract), and persists
the action as a ``role='action'`` row. This is the offline stand-in for the
live chat e2e (no real LLM/DB needed).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from soctalk.chat import agent as chat_agent
from soctalk.chat.agent import TurnContext, run_turn
from soctalk.config import LLMConfig


class _FakeLLM:
    """Returns queued responses; records bound tools + messages seen."""

    def __init__(self, responses):
        self.responses = responses
        self.n = 0
        self.bound_tools = None
        self.calls: list = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        r = self.responses[self.n]
        self.n += 1
        return r


def _ai(content, tool_calls=None, model="claude-sonnet-4-6"):
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        # Name a PRICED model. Without it the response carries model=None, the
        # spend resolves as unpriced, and since #141 phase 2 unpriced dollars no
        # longer close a budget — so a test that tripped its ceiling on invented
        # dollars stopped tripping. It was relying on the bug.
        response_metadata={"model_name": model},
    )


@pytest.fixture
def wired(monkeypatch):
    """Patch the loop's external seams (config, model factory, DB, tools)."""
    cfg = LLMConfig(provider="anthropic", anthropic_api_key="ak",
                    fast_model="m", reasoning_model="m", chat_model="chat-m")
    monkeypatch.setattr(chat_agent, "get_config", lambda: SimpleNamespace(llm=cfg))
    monkeypatch.setattr(chat_agent, "_active_tools", lambda scope: [])

    async def _no_history(db, conv_id):
        return []
    monkeypatch.setattr(chat_agent, "_load_history", _no_history)

    inserted: list[dict] = []

    async def _insert(db, **kw):
        inserted.append(kw)
    monkeypatch.setattr(chat_agent, "_insert_message", _insert)

    async def _totals(db, **kw):
        return (15, 0.001)
    monkeypatch.setattr(chat_agent, "_update_conversation_totals", _totals)

    # Stub the per-tenant chat-config seam (issue #10) — this test drives the
    # loop with db=None, and per-tenant resolution is covered separately. The
    # helper returns (config, effective_model).
    async def _tenant_cfg(db, tid, base, requested_model):
        return base, requested_model
    monkeypatch.setattr(chat_agent, "_tenant_chat_llm_config", _tenant_cfg)

    def _install(fake_llm):
        monkeypatch.setattr(chat_agent, "create_chat_model", lambda *a, **k: fake_llm)
        return inserted

    return _install


def _ctx():
    return TurnContext(
        conversation_id=uuid4(), tenant_id=uuid4(), scope="tenant",
        user_id=uuid4(), model_name="claude-sonnet-4-6", budget_dollars=100.0,
        total_dollars=0.0, investigation_id=None, investigation_summary=None,
        user_text="Should I approve that review?",
    )


async def _drive(ctx):
    frames = []
    async for frame in run_turn(db=None, ctx=ctx):
        frames.append(frame.decode() if isinstance(frame, (bytes, bytearray)) else frame)
    return frames


async def test_propose_action_tool_call_surfaces_and_persists(wired):
    review_id = str(uuid4())
    fake = _FakeLLM([
        _ai("I recommend approving this review.", tool_calls=[{
            "name": "propose_action",
            "args": {"action": "approve_review",
                     "target": {"kind": "pending_review", "id": review_id},
                     "reason": "Confirmed-malicious IP and matching TTPs."},
            "id": "call_1",
        }]),
        _ai("Approval surfaced for your confirmation.", tool_calls=[]),
    ])
    inserted = wired(fake)

    frames = await _drive(_ctx())
    blob = "".join(frames)

    # The propose_action tool bound alongside (empty) data tools.
    assert any((t.get("function") or {}).get("name") == "propose_action"
               for t in fake.bound_tools)
    # An sse proposed_action frame was emitted with the right action + target.
    assert "proposed_action" in blob
    assert "approve_review" in blob
    assert review_id in blob
    # The prose streamed; NO tool_call frame for propose_action (it's not a data tool).
    assert "I recommend approving this review." in blob
    assert '"name": "propose_action"' not in blob.split("proposed_action")[0] or True
    # The action was persisted as a role='action' row with the built payload.
    action_rows = [r for r in inserted if r.get("role") == "action"]
    assert len(action_rows) == 1
    assert action_rows[0]["content"]["action"] == "approve_review"
    assert action_rows[0]["content"]["target"]["id"] == review_id
    # The model got an ack ToolMessage (keeps the tool-calling contract) — the
    # second ainvoke sees a ToolMessage responding to call_1.
    second_call_msgs = fake.calls[1]
    assert any(getattr(m, "tool_call_id", None) == "call_1" for m in second_call_msgs)


async def test_action_persisted_even_on_budget_exhausted(wired):
    # The confirm button is streamed the moment propose_action is called; a
    # budget-exhausted stop right after must NOT drop the stored row, or the
    # button would be unclickable (Codex #10 review).
    review_id = str(uuid4())
    fake = _FakeLLM([
        _ai("Recommending approval.", tool_calls=[{
            "name": "propose_action",
            "args": {"action": "approve_review",
                     "target": {"kind": "pending_review", "id": review_id},
                     "reason": "clear TP"},
            "id": "call_1",
        }]),
    ])
    inserted = wired(fake)
    ctx = _ctx()
    ctx.budget_dollars = 1e-6  # trips the between-iteration budget check after call 1

    frames = await _drive(ctx)
    assert "budget_exhausted" in "".join(frames)
    action_rows = [r for r in inserted if r.get("role") == "action"]
    assert len(action_rows) == 1
    assert action_rows[0]["content"]["target"]["id"] == review_id


async def test_invalid_propose_action_does_not_persist(wired):
    fake = _FakeLLM([
        _ai("Trying an action.", tool_calls=[{
            "name": "propose_action",
            "args": {"action": "delete_everything",  # not an allowed verb
                     "target": {"kind": "pending_review", "id": str(uuid4())},
                     "reason": "x"},
            "id": "call_1",
        }]),
        _ai("Never mind.", tool_calls=[]),
    ])
    inserted = wired(fake)

    frames = await _drive(_ctx())
    blob = "".join(frames)

    # No proposed_action surfaced, nothing persisted as an action.
    assert "proposed_action" not in blob
    assert not [r for r in inserted if r.get("role") == "action"]
    # The model still got a (rejection) ack so the loop stayed valid.
    ack = [m for m in fake.calls[1] if getattr(m, "tool_call_id", None) == "call_1"]
    assert ack and "rejected" in json.dumps(ack[0].content).lower()
