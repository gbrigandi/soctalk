#!/usr/bin/env python3
"""Benchmark HOSTED OpenAI-compatible model APIs against soctalk's triage eval.

The managed-endpoint counterpart to ``bench/run_bench.py`` (which self-hosts open
models on Modal GPUs). This points ``python -m soctalk.evals.triage --json`` at
hosted OpenAI-compatible APIs — DeepSeek, Qwen (Alibaba DashScope), a frontier
baseline — so you can compare Chinese / open models on soctalk's ACTUAL triage
output contract (routing action + verdict + confidence) without deploying
anything. This is the ``hosted-API`` axis of #33, and the small compatibility
matrix Richard suggested on #4 (DeepSeek / Qwen vs a managed endpoint vs
frontier).

It exercises the seams shipped this program:
  * #4  — per-tier / OpenAI-compatible provider via SOCTALK_LLM_PROVIDER=openai +
          OPENAI_BASE_URL, and (``--mixed``) the SOCTALK_<TIER>_* per-tier env.
  * #125 — prices come from the install catalog now, not an env overlay. The
          eval drives graph nodes directly with no run row, so a model outside
          the built-in table prices at the fail-expensive fallback here. The
          ``prices`` below are printed for context and no longer configure the
          app; seed the catalog (``soctalk-prices import``) if you want the
          eval's own dollar figures to be right.
  * #9  — the golden triage eval drives the REAL supervisor + verdict nodes; no
          real tenant data leaves the machine.

Set the key(s) for whichever endpoints you want to run, then:
    export DEEPSEEK_API_KEY=sk-...      # https://platform.deepseek.com
    export DASHSCOPE_API_KEY=sk-...     # optional, Qwen via Alibaba DashScope
    # ANTHROPIC_API_KEY from your .env powers the frontier baseline.

    python bench/run_hosted.py                    # every endpoint with a key set
    python bench/run_hosted.py --only deepseek-chat frontier
    python bench/run_hosted.py --mixed            # + a fast=DeepSeek / reasoning=frontier row (#4)
    python bench/run_hosted.py --trials 3         # consistency across trials

Prices below are APPROXIMATE public list values (USD / Mtok) for cost context —
verify against the vendor before drawing spend conclusions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# One row per hosted model config. ``provider`` picks the client; for
# openai-compatible endpoints ``base_url`` is the API root (``/v1`` appended when
# missing). ``prices`` is context for the printed table (#125 retired the env
# overlay it used to feed). ``key_env`` names the env var holding the credential.
ENDPOINTS: list[dict] = [
    {
        # DeepSeek's non-thinking "flash" model — the successor to the
        # deepseek-chat alias (Codex flagged the alias as deprecating ~2026-07-24).
        # NOTE: verify the exact id against GET /models with your key — vendor
        # naming shifts; run_hosted logs the model it used.
        "label": "deepseek-flash",
        "provider": "openai", "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "fast": "deepseek-v4-flash", "reasoning": "deepseek-v4-flash",
        "prices": {"deepseek-v4-flash": {"input": 0.27, "output": 1.10}},
    },
    {
        "label": "deepseek-chat",  # compat row for the legacy alias
        "provider": "openai", "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "fast": "deepseek-chat", "reasoning": "deepseek-chat",
        "prices": {"deepseek-chat": {"input": 0.27, "output": 1.10}},
    },
    {
        "label": "deepseek-reasoner",
        "provider": "openai", "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        # fast loop on the cheap chat model, final verdict on the reasoner.
        "fast": "deepseek-chat", "reasoning": "deepseek-reasoner",
        "prices": {"deepseek-chat": {"input": 0.27, "output": 1.10},
                   "deepseek-reasoner": {"input": 0.55, "output": 2.19}},
    },
    {
        "label": "qwen-plus",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode",
        "key_env": "DASHSCOPE_API_KEY",
        "fast": "qwen-plus", "reasoning": "qwen-max",
        "prices": {"qwen-plus": {"input": 0.4, "output": 1.2},
                   "qwen-max": {"input": 1.6, "output": 6.4}},
    },
    {
        "label": "frontier",
        "provider": "anthropic", "base_url": None,
        "key_env": "ANTHROPIC_API_KEY",
        "fast": "claude-sonnet-4-6", "reasoning": "claude-sonnet-4-6",
        "prices": {},  # in the built-in table already
    },
]


def _base_env(ep: dict, key: str) -> dict:
    """Env for a single-provider endpoint (both tiers on the same backend)."""
    env = {**os.environ,
           "SOCTALK_EVAL_ROUTING_THRESHOLD": "0",  # collect scores, never abort
           "SOCTALK_EVAL_VERDICT_THRESHOLD": "0",
           }
    if ep["provider"] == "anthropic":
        env["SOCTALK_LLM_PROVIDER"] = "anthropic"
        env["ANTHROPIC_API_KEY"] = key
        env["OPENAI_API_KEY"] = ""  # present-but-empty blocks .env re-add (dotenv override=False)
        env["OPENAI_BASE_URL"] = ""
    else:
        env["SOCTALK_LLM_PROVIDER"] = "openai"
        base = ep["base_url"]
        env["OPENAI_BASE_URL"] = base if base.endswith("/v1") else f"{base}/v1"
        env["OPENAI_API_KEY"] = key
        env["ANTHROPIC_API_KEY"] = ""  # blank so the mutual-exclusion guard picks openai
        env["ANTHROPIC_BASE_URL"] = ""
    env["SOCTALK_FAST_MODEL"] = ep["fast"]
    env["SOCTALK_REASONING_MODEL"] = ep["reasoning"]
    return env


def _mixed_env(fast_ep: dict, fast_key: str, reason_ep: dict, reason_key: str) -> dict:
    """Env for a hybrid tenant (#4): fast/router tier and reasoning tier on
    different providers, wired through the SOCTALK_<TIER>_* per-tier seam."""
    prices = {**fast_ep["prices"], **reason_ep["prices"]}
    env = {**os.environ,
           "SOCTALK_EVAL_ROUTING_THRESHOLD": "0", "SOCTALK_EVAL_VERDICT_THRESHOLD": "0",
           # Global/default provider = the reasoning tier's; the fast tier
           # overrides via SOCTALK_FAST_*.
           "SOCTALK_LLM_PROVIDER": reason_ep["provider"],
           "SOCTALK_FAST_PROVIDER": fast_ep["provider"],
           "SOCTALK_FAST_MODEL": fast_ep["fast"],
           "SOCTALK_FAST_BASE_URL": (fast_ep["base_url"] or "").rstrip("/") +
                                    ("" if (fast_ep["base_url"] or "").endswith("/v1") else "/v1"),
           "SOCTALK_FAST_API_KEY": fast_key,
           "SOCTALK_REASONING_PROVIDER": reason_ep["provider"],
           "SOCTALK_REASONING_MODEL": reason_ep["reasoning"]}
    # Per-tier keys carry the credentials; blank the globals so the guard relaxes
    # on the per-tier providers (both keys would otherwise be ambiguous).
    if reason_ep["provider"] == "anthropic":
        env["ANTHROPIC_API_KEY"] = reason_key
        env["OPENAI_API_KEY"] = ""
    else:
        env["OPENAI_API_KEY"] = reason_key
        env["ANTHROPIC_API_KEY"] = ""
    return env


def run_eval(label: str, env: dict, trials: int, concurrency: int) -> dict | None:
    print(f"  running triage eval: {label} ...", flush=True)
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "soctalk.evals.triage",
         "--json", "--label", label, "--trials", str(trials),
         "--concurrency", str(concurrency)],
        env=env, capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    elapsed = time.monotonic() - t0
    sys.stderr.write(proc.stderr)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        out["_elapsed_s"] = elapsed
        return out
    except (json.JSONDecodeError, IndexError):
        print(f"  ! {label}: eval produced no JSON (endpoint/auth error?) — skipped",
              flush=True)
        return None


def print_table(rows: list[dict]) -> None:
    print("\n" + "=" * 100)
    print("HOSTED-API BENCHMARK  (soctalk triage golden set: 9 cases — routing x3, verdict x6)")
    print("=" * 100)
    print(f"{'model':22s} {'routing':>16s} {'verdict':>16s} {'schema-err':>11s} "
          f"{'wall-s':>8s} {'$/Mtok in/out':>16s}")
    print("-" * 100)
    for r in rows:
        s = r["result"]["summary"]
        rt = s.get("routing", {})
        vd = s.get("verdict", {})
        rt_s = f"{rt.get('passed', 0)}/{rt.get('total', 0)} ({rt.get('accuracy', 0):.2f})" if rt else "-"
        vd_s = f"{vd.get('passed', 0)}/{vd.get('total', 0)} ({vd.get('accuracy', 0):.2f})" if vd else "-"
        schema_err = rt.get("schema_errors", 0) + vd.get("schema_errors", 0)
        print(f"{r['label']:22s} {rt_s:>16s} {vd_s:>16s} {schema_err:>11d} "
              f"{r['result']['_elapsed_s']:>8.1f} {r['price']:>16s}")
    print("-" * 100)
    print("schema-err = trials where the model couldn't produce the structured-output contract "
          "(the key open-model risk).")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python bench/run_hosted.py")
    p.add_argument("--only", nargs="*", default=None,
                   help="Subset of endpoint labels to run (default: all with a key set).")
    p.add_argument("--mixed", action="store_true",
                   help="Also run a hybrid row: fast=DeepSeek / reasoning=frontier (#4).")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=4)
    args = p.parse_args(argv)

    lineup = ENDPOINTS if not args.only else [e for e in ENDPOINTS if e["label"] in args.only]
    rows: list[dict] = []

    for ep in lineup:
        key = os.getenv(ep["key_env"], "").strip()
        if not key:
            print(f"skip {ep['label']}: {ep['key_env']} not set", flush=True)
            continue
        res = run_eval(ep["label"], _base_env(ep, key), args.trials, args.concurrency)
        if res is None:
            continue
        rp = ep["prices"].get(ep["reasoning"]) or ep["prices"].get(ep["fast"])
        price = f"{rp['input']}/{rp['output']}" if rp else "built-in"
        rows.append({"label": ep["label"], "result": res, "price": price})

    if args.mixed:
        ds = next((e for e in ENDPOINTS if e["label"] == "deepseek-chat"), None)
        fr = next((e for e in ENDPOINTS if e["label"] == "frontier"), None)
        ds_key = os.getenv(ds["key_env"], "").strip() if ds else ""
        fr_key = os.getenv(fr["key_env"], "").strip() if fr else ""
        if ds and fr and ds_key and fr_key:
            env = _mixed_env(ds, ds_key, fr, fr_key)
            res = run_eval("mixed(fast=DS,reason=frontier)", env, args.trials, args.concurrency)
            if res is not None:
                rows.append({"label": "mixed DS→frontier", "result": res, "price": "0.27/15"})
        else:
            print("skip --mixed: needs both DEEPSEEK_API_KEY and ANTHROPIC_API_KEY", flush=True)

    if not rows:
        print("\nNo endpoints ran — set at least one key (e.g. DEEPSEEK_API_KEY) and retry.")
        return 2
    print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
