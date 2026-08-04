#!/usr/bin/env python3
"""Live e2e for #77 Phase 1 against a real scale-to-zero RunPod endpoint.

Proves, against actual RunPod behavior (not a mock), that:
  1. a WARM serverless endpoint serves a normal InferenceResult through
     SocTalk's openai-compatible path;
  2. resolve_backend classifies the RunPod endpoint as scale_to_zero;
  3. a COLD endpoint (scaled to zero) hit with a short client timeout raises
     ServerlessUnavailableError from ainvoke_request (the profile-scoped
     cold-start reclassification), which classify_llm_error maps to
     ``serverless_unavailable`` — i.e. the worker would RELEASE + retry the
     run instead of failing it terminally.

Env: RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID (from .env).
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx
from langchain_core.messages import HumanMessage

from soctalk.config import LLMConfig
from soctalk.inference import (
    InferenceAccounting,
    InferenceRequest,
    InferenceTier,
    ainvoke_request,
    resolve_backend,
)
from soctalk.llm import ServerlessUnavailableError, classify_llm_error

KEY = os.environ["RUNPOD_API_KEY"]
EID = os.environ["RUNPOD_ENDPOINT_ID"]
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
BASE = f"https://api.runpod.ai/v2/{EID}/openai/v1"
HEALTH = f"https://api.runpod.ai/v2/{EID}/health"


def _cfg(timeout: float) -> LLMConfig:
    return LLMConfig(
        provider="openai",
        openai_api_key=KEY,
        openai_base_url=BASE,
        fast_model=MODEL,
        reasoning_model=MODEL,
        timeout_seconds=timeout,
        max_retries=0,  # single shot: we want to SEE the cold failure, not SDK-retry it away
    )


def _req() -> InferenceRequest:
    return InferenceRequest(
        tier=InferenceTier.ROUTER,
        metadata=InferenceAccounting(producer="runpod-e2e", budget_state=None),
        messages=[HumanMessage(content="reply with the single word OK")],
    )


def _workers() -> dict:
    r = httpx.get(HEALTH, headers={"Authorization": f"Bearer {KEY}"}, timeout=10)
    return r.json().get("workers", {})


async def main() -> int:
    # --- profile classification (property 2) ---
    rb = resolve_backend(_cfg(120), InferenceTier.ROUTER)
    print(f"[profile] backend_id={rb.profile.backend_id} readiness={rb.profile.readiness}")
    assert rb.profile.readiness == "scale_to_zero", "RunPod endpoint must resolve scale_to_zero"
    print("  PASS: RunPod endpoint classified scale_to_zero\n")

    # --- WARM call (property 1) ---
    print(f"[warm] workers={_workers()}  calling with 120s timeout ...")
    t0 = time.time()
    res = await ainvoke_request(_req(), cfg=_cfg(120))
    txt = getattr(res.content, "content", res.content)
    print(f"  PASS: warm InferenceResult in {time.time()-t0:.1f}s, content={str(txt)[:60]!r}\n")

    # --- scale to zero, then COLD call (property 3) ---
    # Two review findings live in this block (Codex, #77). The wait loop used
    # to print "endpoint at zero" whether it broke on zero or merely expired,
    # and a call that SUCCEEDED fell through both except arms straight to
    # "ALL ASSERTIONS PASSED" — a warm endpoint produced a green run of a
    # script whose whole purpose is to demonstrate a cold failure. A proof
    # script that cannot fail on the case it exists for proves nothing, so
    # both paths are now explicit failures.
    print("[cold] waiting for scale-to-zero (idleTimeout=60s) ...")
    reached_zero = False
    for _ in range(30):
        w = _workers()
        active = w.get("idle", 0) + w.get("ready", 0) + w.get("running", 0) + w.get("initializing", 0)
        print(f"  workers={w}")
        if active == 0:
            reached_zero = True
            break
        await asyncio.sleep(15)
    if not reached_zero:
        print("  FAIL: endpoint never reached zero workers; a cold-start trial "
              "against a warm endpoint is not evidence. Lower idleTimeout or wait.")
        return 1
    print("[cold] endpoint at zero; calling with SHORT 8s timeout to force the cold-start failure ...")
    t0 = time.time()
    try:
        await ainvoke_request(_req(), cfg=_cfg(8))
        print(f"  FAIL: cold call SUCCEEDED in {time.time()-t0:.1f}s — the endpoint "
              "was not cold (a worker served inside the short timeout). Nothing "
              "about cold-start classification was exercised.")
        return 1
    except ServerlessUnavailableError as e:
        cat = classify_llm_error(e)
        cause = e.__cause__
        print(f"  raised ServerlessUnavailableError in {time.time()-t0:.1f}s -> classify={cat!r}")
        # The raw gateway signal is the evidence the whole #77 dispute needs;
        # the wrapped exception alone hides it.
        print(f"  raw cause: {type(cause).__name__} "
              f"status={getattr(cause, 'status_code', None)} str={str(cause)[:200]!r}")
        assert cat == "serverless_unavailable"
        print("  PASS: cold RunPod call reclassified transient -> worker would RELEASE + retry\n")
    except Exception as e:  # noqa: BLE001
        cat = classify_llm_error(e)
        print(f"  FAIL: expected ServerlessUnavailableError, got {type(e).__name__}: {e} (classify={cat})")
        return 1

    print("ALL LIVE E2E ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
