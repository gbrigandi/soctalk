"""Entry point for the per-tenant runs-worker.

Loop: claim → ainvoke graph → heartbeat → complete. One tenant per
process; tenant identity comes from the worker token's claims, not env
or chart values. Token is mounted as a K8s Secret at
``/run/secrets/runs-worker/token``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("soctalk.runs_worker")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

VERSION = "0.2.0"


def _post_attempts() -> int:
    """Transport retry count for _post_complete/_post_release. Tolerates a
    malformed WORKER_COMPLETE_ATTEMPTS (e.g. "three") instead of crashing the
    finalize path — a crash there would skip the release POST, leaving the run
    active-but-uncounted until the lease reaper, so cold-starts could retry past
    the intended cap. Clamped to >= 1."""
    raw = os.environ.get("WORKER_COMPLETE_ATTEMPTS", "3")
    try:
        return max(1, int(str(raw).strip()))
    except (ValueError, AttributeError):
        logger.warning("bad WORKER_COMPLETE_ATTEMPTS=%r -> default 3", raw)
        return 3
HEARTBEAT_INTERVAL_SECONDS = 20


def _read_token() -> str:
    path = Path(
        os.environ.get(
            "WORKER_TOKEN_PATH", "/run/secrets/runs-worker/token"
        )
    )
    return path.read_text().strip()


def _api_url() -> str:
    return os.environ["SOCTALK_API_URL"].rstrip("/")


def _disposition_from_final(final: dict[str, Any], run_status: str) -> str:
    """Map graph terminal state → case disposition L1 should apply.

    Returns one of:
      - ``"close_fp"``: close as auto-determined false positive
      - ``"escalate"``: keep case active, flag for human review
      - ``"leave_open"``: no change (budget halt, error, needs-more-info)

    The graph reaches a terminal state via the ``close_investigation``
    node, which is entered either from the supervisor's CLOSE action
    (low TP confidence, auto-FP) or from the verdict node's
    escalate/close/needs_more_info routing. ``needs_more_info`` plus
    high supervisor TP confidence means "agent saw real signal but
    can't auto-decide" — that is precisely the escalate-to-human case.
    """
    if run_status != "completed":
        return "leave_open"

    def _enum_value(v: Any) -> str:
        # Handle both raw strings and (str, Enum) instances. Pydantic
        # v2 ``model_dump()`` keeps enum subclass instances in the dict
        # rather than coercing them to bare strings, so a naive
        # ``str(v).lower()`` returns ``"verdictdecision.close"``.
        if hasattr(v, "value"):
            return str(v.value).lower()
        return str(v or "").lower()

    verdict = final.get("verdict") or {}
    decision = _enum_value(verdict.get("decision"))
    sup = final.get("supervisor_decision") or {}
    sup_conf = float(sup.get("tp_confidence") or 0.0)
    sup_action = _enum_value(sup.get("next_action")).upper()

    # Guard interrupt (#45): the LLM draft stands but a human must sign off
    # before it takes effect. The worker has no HIL of its own — without a
    # checkpointer the graph's human_review ends the run untouched — so the
    # sign-off queue IS the pending_reviews row an escalate disposition
    # creates. The draft rides along in the verdict summary/audit.
    if final.get("verdict_interrupted"):
        return "escalate"

    # Verdict node fired — trust its decision.
    if decision == "escalate":
        return "escalate"
    if decision == "close":
        return "close_fp"
    if decision == "needs_more_info":
        # ``needs_more_info`` is the AI explicitly asking for analyst
        # review — escalate unconditionally. Previously this required
        # supervisor confidence >= 0.7, but in practice a verdict that
        # cannot auto-resolve always benefits from a human gate;
        # leaving low-confidence cases as ``leave_open`` strands them
        # in the queue with no resolution path.
        return "escalate"

    # No verdict — supervisor short-circuited.
    if sup_action == "CLOSE":
        return "close_fp"
    return "leave_open"


def _verdict_summary(final: dict[str, Any]) -> str | None:
    verdict = final.get("verdict") or {}
    rec = verdict.get("recommendation")
    if rec and final.get("verdict_interrupted"):
        # An interrupted draft reaches the reviewer with its close-shaped
        # recommendation intact — say why it's in the queue (#45).
        return (f"[SIGN-OFF REQUIRED: guard interrupted this draft] {rec}")[:1024]
    if rec:
        return str(rec)[:1024]
    sup = final.get("supervisor_decision") or {}
    reasoning = sup.get("action_reasoning")
    if reasoning:
        return str(reasoning)[:1024]
    return None


def _verdict_confidence(final: dict[str, Any]) -> float | None:
    """Float in [0, 1] from the reasoning LLM verdict, or None."""
    verdict = final.get("verdict") or {}
    conf = verdict.get("confidence")
    if conf is None:
        return None
    try:
        f = float(conf)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return None


def _verdict_findings(final: dict[str, Any]) -> list[str]:
    """Extract analyst-readable findings from the verdict + investigation state.

    Combines the verdict's ``key_evidence`` (LLM-asserted facts), any
    investigation-level findings (from the wazuh_worker correlation), and
    falls back to ``gaps_in_evidence`` when the verdict was ``needs_more_info``
    so the analyst sees *what the AI was missing*, not just an empty list.
    """
    out: list[str] = []
    seen: set[str] = set()
    verdict = final.get("verdict") or {}
    for k in ("key_evidence", "gaps_in_evidence", "assumptions_made"):
        for item in verdict.get(k) or []:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s[:300])
    inv = final.get("investigation") or {}
    for f in inv.get("findings") or []:
        # ``findings`` from wazuh_worker is a list of dicts with
        # ``description`` and ``severity``.
        if isinstance(f, dict):
            desc = f.get("description") or ""
            sev = f.get("severity") or ""
            line = f"[{sev}] {desc}".strip() if sev else desc
        else:
            line = str(f)
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line[:300])
    return out[:20]


def _verdict_enrichments(final: dict[str, Any]) -> dict[str, Any]:
    """Pull observable + enrichment context for the review queue row.

    Includes Cortex / MISP outputs when present, plus a summary of
    observables flagged by upstream workers.
    """
    out: dict[str, Any] = {}
    inv = final.get("investigation") or {}
    if inv.get("enrichments"):
        out["analyzer_results"] = inv["enrichments"][:20]
    if inv.get("observables"):
        out["observables"] = [
            {
                "type": o.get("type"),
                "value": o.get("value"),
                "verdict": o.get("verdict"),
            }
            for o in (inv["observables"] or [])[:20]
            if isinstance(o, dict)
        ]
    verdict = final.get("verdict") or {}
    if verdict.get("threat_assessment"):
        out["threat_assessment"] = str(verdict["threat_assessment"])[:600]
    if verdict.get("evidence_strength"):
        out["evidence_strength"] = str(verdict["evidence_strength"])
    if verdict.get("potential_impact"):
        out["potential_impact"] = str(verdict["potential_impact"])
    if verdict.get("urgency"):
        out["urgency"] = str(verdict["urgency"])
    return out


def _wazuh_level_to_severity(level: int) -> str:
    if level >= 12:
        return "critical"
    if level >= 8:
        return "high"
    if level >= 5:
        return "medium"
    return "low"


def _build_state(claim: dict[str, Any]) -> dict[str, Any]:
    """Shape an L1 claim response into a SecOps graph state.

    The supervisor reads ``state["investigation"]["alerts"]`` and
    ``state["investigation"]["observables"]`` to build its context. The
    L1 claim payload uses the IR alert schema (``rule.id``, ``rule.level``,
    ``signature``, ``asset_ids``, ``initial_iocs``); we project that into
    the dict shape the supervisor's prompt-builder expects.
    """
    import re as _re
    from datetime import datetime

    def _project(alert: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
        rule = alert.get("rule") or {}
        level = int(rule.get("level") or 0)
        asset_ids = list(alert.get("asset_ids") or [])
        iocs_in = [
            i for i in (alert.get("initial_iocs") or [])
            if isinstance(i, dict) and i.get("value")
        ]
        obs = [
            {"type": i.get("type", "unknown"), "value": i.get("value", ""),
             "source": f"alert:{alert.get('id', '')}"}
            for i in iocs_in
        ]
        rule_desc = (
            alert.get("description") or alert.get("signature")
            or rule.get("id") or "unknown rule"
        )
        raw_log = str(alert.get("description") or "")
        asset_name = asset_ids[1] if len(asset_ids) > 1 else (
            asset_ids[0] if asset_ids else "unknown"
        )
        mm = _re.search(r"\bon\s+([A-Z][A-Z0-9-]{2,32})[:\s]", raw_log + " " + rule_desc)
        if mm:
            asset_name = mm.group(1)
        return (
            {
                "id": str(alert.get("id", claim["run_id"])),
                "adapter_source": alert.get("adapter_source") or "wazuh",
                "severity": _wazuh_level_to_severity(level),
                "level": level,
                "rule_id": rule.get("id"),
                "rule_description": rule_desc,
                "mitre": alert.get("mitre") or {},
                "rule_groups": alert.get("rule_groups") or [],
                "entities": alert.get("entities") or [],
                "source": {"agent_id": asset_name, "agent_name": asset_name},
                "timestamp": datetime.now(UTC).isoformat(),
                "raw_data": alert,
                "observables": obs,
            },
            obs,
        )

    # Multi-alert (issue #26): reason over every correlated alert #27
    # grouped onto the investigation, not just the primary. Cap to bound
    # prompt size; the claim already orders by severity desc so the cap
    # keeps the most severe. Observables deduped across alerts.
    raw_alerts = claim.get("alerts") or [claim["alert"]]
    max_alerts = int(os.environ.get("SOCTALK_MAX_ALERTS_PER_RUN", "20"))
    raw_alerts = raw_alerts[:max_alerts]
    alert = claim["alert"]  # primary (highest-severity) — kept for compat below

    supervisor_alerts: list[dict[str, Any]] = []
    seen_obs: set[tuple[str, str]] = set()
    observables: list[dict] = []
    for a in raw_alerts:
        sa, obs = _project(a)
        supervisor_alerts.append(sa)
        for o in obs:
            k = (o["type"], o["value"])
            if k not in seen_obs:
                seen_obs.add(k)
                observables.append(o)

    level = max((sa["level"] for sa in supervisor_alerts), default=0)
    supervisor_alert = supervisor_alerts[0]
    pending_observables = [{**o, "source": "wazuh"} for o in observables]

    # Demo-mode TI seeding, OFF unless SOCTALK_DEMO_TI_SEEDING is set.
    # When L2 runs without cortex/MISP (chart components.cortex.enabled=false)
    # there is no enrichment service to score the IOCs, so the supervisor stays
    # at low TP confidence and never escalates. For demos this block invents
    # enrichment evidence from the upstream Wazuh rule level:
    #   level >= 13 → seed malicious enrichments + MISP attribution
    #   level 10-12 → leave un-enriched, supervisor decides on alert context
    # The seeded verdicts, analyzers, detection ratios and actor attribution are
    # FABRICATED. They must never reach a real investigation, so the default is
    # off and every seeded object is tagged synthetic=True for the record.
    # Only Wazuh alerts are eligible: rule levels from other adapter sources do
    # not carry Wazuh's threat semantics.
    enrichments: list[dict] = []
    findings: list[dict] = []
    misp_context: dict[str, Any] = {}
    pending = list(pending_observables)
    seeding_enabled = os.environ.get("SOCTALK_DEMO_TI_SEEDING", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    wazuh_only = bool(supervisor_alerts) and all(
        sa.get("adapter_source") == "wazuh" for sa in supervisor_alerts
    )
    if seeding_enabled and wazuh_only and level >= 13 and observables:
        for i, o in enumerate(observables):
            enrichments.append({
                "observable": o,
                "verdict": "malicious",
                "analyzer": "VirusTotal" if i % 2 == 0 else "AlienVault OTX",
                "confidence": 0.95,
                "tags": ["confirmed-malicious", "ioc", "synthetic-demo-data"],
                "synthetic": True,
                "details": {
                    "detection_ratio": "62/72",
                    "first_submission": "2024-09-15",
                    "associated_malware": "Mimikatz",
                },
            })
        misp_context = {
            "synthetic": True,
            "checked_iocs": [o["value"] for o in observables],
            "matches": [
                {
                    "value": o["value"],
                    "type": o["type"],
                    "to_ids": True,
                    "event_ids": ["12345", "12678"],
                }
                for o in observables
            ],
            "threat_actors": ["APT29 (Cozy Bear)"],
            "campaigns": ["NOBELIUM credential harvesting"],
            "warninglist_hits": [],
        }
        findings.append({
            "severity": "critical",
            "synthetic": True,
            "description": (
                f"Confirmed credential dumping on critical asset "
                f"{supervisor_alert['source']['agent_name']}; "
                f"observables match known APT29 infrastructure"
            ),
            "evidence": [
                f"{o['type']}={o['value']} matched VirusTotal/OTX with"
                f" verdict=malicious, confidence>0.9"
                for o in observables
            ],
            "mitre": ["T1003.001"],
        })
        pending = []

    investigation: dict[str, Any] = {
        "id": claim["run_id"],
        "alerts": supervisor_alerts,
        "enrichments": enrichments,
        "findings": findings,
        "observables": observables,
        "enriched_observables": [
            e["observable"] for e in enrichments
        ],
        "misp_context": misp_context,
    }
    # Authorization context (epic M1, fixture-fed): a typed AuthorizationContext may ride in
    # on the claim payload, or be seeded from a fixture file for demos/evals. Invalid payloads
    # are dropped (bad evidence is no evidence) — never rendered half-parsed into a prompt.
    authorization_context = _load_authorization_context(claim)
    if authorization_context is not None:
        investigation["authorization_context"] = authorization_context

    return {
        "investigation_id": claim["run_id"],
        "investigation": investigation,
        "alert": alert,
        "iteration_count": 0,
        "events": [alert],
        "pending_observables": pending,
        "tokens_used": int(claim["tokens_used"]),
        # Per-run token budget: same env-first precedence as dollars, so a
        # per-tenant SOCTALK_CASE_RUN_TOKEN_BUDGET (rendered from the tenant's
        # llm_token_budget_per_run) actually overrides the DB row default —
        # previously the claim value was seeded unconditionally and the env was
        # ignored for claimed runs.
        **_tokens_budget_kv(claim.get("tokens_budget")),
        "dollars_used": float(claim.get("dollars_used") or 0.0),
        # The run row is the only source; see _dollars_budget_kv.
        **_dollars_budget_kv(claim.get("dollars_budget")),
        # Rates this run is priced at, frozen when the run was created (#125).
        # Absent for a run created before the column existed, or by an API that
        # predates it, in which case pricing falls back to the built-in table
        # and env overlay exactly as before.
        **(
            {"price_snapshot": claim["price_snapshot"]}
            if claim.get("price_snapshot")
            else {}
        ),
    }


_AUTHZ_FIXTURE_CACHE: dict[str, dict[str, Any] | None] = {}


def _load_authorization_context(claim: dict[str, Any]) -> dict[str, Any] | None:
    """The fixture-fed M1 authorization seam: claim payload first, then the optional
    SOCTALK_AUTHZ_FIXTURE file (a JSON/YAML AuthorizationContext applied to every run —
    the authorization analogue of the demo-mode TI seeding above). Returns a validated
    plain dict (the graph state is dict-shaped), or None."""
    from pydantic import ValidationError

    from soctalk.models.authorization import AuthorizationContext

    raw = claim.get("authorization_context")
    if raw is None:
        fixture_path = os.environ.get("SOCTALK_AUTHZ_FIXTURE", "")
        if not fixture_path:
            return None
        if fixture_path not in _AUTHZ_FIXTURE_CACHE:
            try:
                text = Path(fixture_path).read_text()
                if fixture_path.endswith((".yaml", ".yml")):
                    import yaml

                    _AUTHZ_FIXTURE_CACHE[fixture_path] = yaml.safe_load(text)
                else:
                    _AUTHZ_FIXTURE_CACHE[fixture_path] = json.loads(text)
            except (OSError, ValueError) as exc:
                logging.getLogger(__name__).warning(
                    "authorization fixture unreadable: %s (%s)", fixture_path, exc
                )
                _AUTHZ_FIXTURE_CACHE[fixture_path] = None
        raw = _AUTHZ_FIXTURE_CACHE[fixture_path]
    if raw is None:
        return None
    try:
        return AuthorizationContext.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        logging.getLogger(__name__).warning(
            "authorization_context invalid (%d errors) — dropping it", exc.error_count()
        )
        return None


def _tokens_budget_kv(claim_tokens_budget: Any) -> dict[str, int]:
    """Resolve the token-budget seed for graph state.

    The CLAIM row wins (#103): the server resolves the effective per-tenant
    budget at run creation and stamps it on the row, so per-tenant budget
    changes take effect with no worker rollout. ``SOCTALK_CASE_RUN_TOKEN_BUDGET``
    is now only an install-wide operator fallback, honoured (if positive) when
    the claim carries no valid budget; a non-positive value is ignored (typo
    guard), falling through to ``token_budget.ensure``'s default.
    """
    try:
        claim_v = int(claim_tokens_budget) if claim_tokens_budget is not None else 0
    except (TypeError, ValueError):
        claim_v = 0
    if claim_v > 0:
        return {"tokens_budget": claim_v}
    env_raw = os.environ.get("SOCTALK_CASE_RUN_TOKEN_BUDGET")
    if env_raw:
        try:
            env_v = int(env_raw)
        except ValueError:
            env_v = 0
        if env_v > 0:
            return {"tokens_budget": env_v}
    return {}


def _dollars_budget_kv(claim_dollars_budget: Any) -> dict[str, float]:
    """Resolve the dollar-budget seed for graph state.

    Returns ``{"dollars_budget": value}`` or ``{}`` (let
    ``token_budget.ensure`` pick the default).

    The run row is the ONLY source. It is resolved from policy and stamped at
    run creation, so it is both what the operator set and what every reader of
    the row sees. SOCTALK_CASE_RUN_DOLLAR_BUDGET used to override it, which is
    how a run capped at $0.0005 came to display $5 to the API, the UI and the
    flight recorder; that env var is gone rather than demoted to a fallback,
    because a second source of truth is exactly what produced the drift.
    """
    try:
        claim_v = float(claim_dollars_budget) if claim_dollars_budget is not None else 0.0
    except (TypeError, ValueError):
        claim_v = 0.0
    return {"dollars_budget": claim_v} if claim_v > 0 else {}


async def _flush_replay_events(
    client: httpx.AsyncClient, run_id: str, lease_id: str, sink: Any
) -> bool:
    """Drain the run's replay-event sink to L1 (issue #72).

    Isolated failure domain: a transient error requeues the batch for the
    next flush; a 409 (lost lease) kills the sink so a reclaimed run's
    re-emission owns the stream. Never raises — replay beats are
    enrichment and must not perturb heartbeats or completion.

    Returns True when nothing remains pending (flushed, rejected-and-
    dropped, or sink dead); False when a transient failure requeued the
    batch — the FINAL flush retries on False, because completion clears
    the lease and a requeued tail (worker floor, verdict) would be lost
    forever.
    """
    batch = sink.drain()
    if not batch:
        return True
    try:
        resp = await client.post(
            f"{_api_url()}/api/internal/worker/runs/{run_id}/events",
            headers={"Authorization": f"Bearer {_read_token()}"},
            json={"lease_id": lease_id, "events": batch},
            timeout=10.0,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("replay_flush_failed run=%s err=%s", run_id, e)
        sink.requeue(batch)
        return False
    if resp.status_code == 409:
        logger.info("replay_flush_not_owned run=%s (409; emission stopped)", run_id)
        sink.kill()
    elif resp.status_code >= 400:
        # Payload-level rejection will not heal on retry — drop the batch.
        logger.warning(
            "replay_flush_rejected run=%s status=%s", run_id, resp.status_code
        )
    return True


async def _heartbeat_loop(
    client: httpx.AsyncClient,
    run_id: str,
    lease_id: str,
    state: dict[str, Any],
    stop: asyncio.Event,
    sink: Any = None,
) -> None:
    token = _read_token()
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS
            )
            return
        except TimeoutError:
            pass
        if sink is not None:
            await _flush_replay_events(client, run_id, lease_id, sink)
        try:
            await client.post(
                f"{_api_url()}/api/internal/worker/runs/{run_id}/heartbeat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "lease_id": lease_id,
                    "tokens_used": int(state.get("tokens_used", 0)),
                    "dollars_used": float(state.get("dollars_used", 0.0)),
                    # Where the dollar figure came from, so the ledger row can
                    # say whether it was measured or inferred (#141 phase 1).
                    "cost_basis": state.get("cost_basis"),
                    "price_source": state.get("price_source"),
                },
                timeout=10.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("heartbeat_failed run=%s err=%s", run_id, e)


# Error categories that mean "the backend was transiently unavailable" rather
# than "this run produced a bad/failed triage". A run that fails on one of these
# is RELEASED for a bounded retry (same run_id) instead of terminally failed, so
# a cold serverless endpoint (#77) never loses an alert.
TRANSIENT_RETRY_CATEGORIES = frozenset({"serverless_unavailable"})

# Categories where re-running the triage plausibly produces a different
# outcome, so the run is worth releasing for the same bounded retry rather
# than failing on the first LLM hiccup: provider blips, rate limits, local
# timeouts, and schema-validation misses (model nondeterminism means a rerun
# can pass). insufficient_credit is deliberately absent from the default — a
# balance does not refill inside the backoff window, and four retries against
# an empty account would only delay the failure a human has to act on anyway.
#
# The cap is the run's max_attempts (migration v1_0039, default 4; settable at
# run creation via SOCTALK_MAX_TRIAGE_ATTEMPTS on the API). Retries reuse the
# SAME run_id through the same /release path as the serverless case, so no
# completed-run side effect can replay, and the per-attempt backoff and
# terminalize-at-cap semantics are the server's, already proven by the
# integration suite.
DEFAULT_RETRIAGE_CATEGORIES = frozenset(
    {"rate_limited", "timeout", "provider_error", "schema_validation", "unknown"}
)


def retriage_categories() -> frozenset[str]:
    """Which LLM-failure categories are released for re-triage.

    ``SOCTALK_RETRIAGE_CATEGORIES`` overrides the default set (comma
    separated); ``off``/``none``/empty disables re-triage entirely, leaving
    only the serverless release. Read per call rather than at import so a
    test or a restart-free config change behaves predictably.
    """
    raw = os.environ.get("SOCTALK_RETRIAGE_CATEGORIES")
    if raw is None:
        return DEFAULT_RETRIAGE_CATEGORIES
    cleaned = raw.strip().lower()
    if cleaned in ("", "off", "none", "0"):
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def releasable_error_category(
    supervisor_category: str | None, verdict_category: str | None,
) -> str | None:
    """The category this run should be released (not failed) under, or None.

    Transient backend unavailability always releases. LLM triage failures
    release when their category is in ``retriage_categories()``. Factored out
    of the completion path so the decision is unit-testable on its own.
    """
    releasable = TRANSIENT_RETRY_CATEGORIES | retriage_categories()
    if supervisor_category in releasable:
        return supervisor_category
    if verdict_category in releasable:
        return verdict_category
    return None


async def _post_release(
    client: httpx.AsyncClient, run_id: str, lease_id: str, category: str,
    tokens_used: int, dollars_used: float,
    cost_basis: str | None = None, price_source: str | None = None,
) -> None:
    """POST a release-for-retry. Same transport tolerance as _post_complete:
    a 409 means we no longer own the run (benign), transport blips are retried
    a few times then abandoned (the lease reaper requeues if uncommitted)."""
    token = _read_token()
    url = f"{_api_url()}/api/internal/worker/runs/{run_id}/release"
    body = json.dumps({
        "lease_id": lease_id, "error_category": category,
        "tokens_used": tokens_used, "dollars_used": dollars_used,
        "cost_basis": cost_basis, "price_source": price_source,
    })
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    attempts = _post_attempts()
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.post(url, headers=headers, content=body, timeout=15.0)
        except Exception as e:  # noqa: BLE001 — transport error; lease reaper covers uncommitted
            if attempt >= attempts:
                logger.warning("release_unconfirmed run=%s err=%s", run_id, e)
                return
            await asyncio.sleep(min(2**attempt, 8))
            continue
        if resp.status_code == 409:
            logger.info("release_not_owned run=%s (409, benign)", run_id)
            return
        if resp.status_code >= 400:
            logger.warning("release_failed run=%s status=%s body=%s",
                           run_id, resp.status_code, resp.text[:300])
            return
        info = resp.json()
        logger.info("run_released run=%s category=%s retrying=%s attempts=%s/%s",
                    run_id, category, info.get("retrying"),
                    info.get("attempts"), info.get("max_attempts"))
        return


async def _post_complete(
    client: httpx.AsyncClient, run_id: str, complete_payload: dict[str, Any]
) -> None:
    """POST the run completion, tolerant of transport blips and lost responses.

    A transient failure on the complete POST must never crash the worker loop
    (issue #61): with concurrent runs one dropped response would otherwise take
    down a whole loop. A ``409`` always means "you no longer own this active
    run" — either this lease already committed on a prior attempt whose response
    was lost, or the lease expired and another worker took over. In both cases
    the correct action is identical: stop touching the run, do not error. If the
    server never committed, the lease reaper requeues the run.
    """
    token = _read_token()
    url = f"{_api_url()}/api/internal/worker/runs/{run_id}/complete"
    # ``findings``/``enrichments`` carry datetimes from the graph's final state;
    # httpx's json= encoder can't serialize those, so stringify with default=str.
    body = json.dumps(complete_payload, default=str)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # Total attempts (not retries-on-top-of-one), so 1 means "try once".
    attempts = _post_attempts()
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.post(url, headers=headers, content=body, timeout=15.0)
        except Exception as e:  # noqa: BLE001 — transport error; may be pre- or post-commit
            if attempt >= attempts:
                logger.warning(
                    "complete_unconfirmed run=%s err=%s "
                    "(giving up; lease expiry requeues if uncommitted)",
                    run_id,
                    e,
                )
                return
            await asyncio.sleep(min(2**attempt, 8))
            continue
        if resp.status_code == 409:
            # 409 = this worker no longer owns an active run: either it already
            # committed (prior lost-response attempt) or the lease expired and
            # another worker reclaimed it. Either way, stop; not an error.
            logger.info("complete_not_owned run=%s (409, benign)", run_id)
            return
        if resp.status_code >= 400:
            logger.warning(
                "complete_failed run=%s status=%s body=%s",
                run_id,
                resp.status_code,
                resp.text[:500],
            )
            return
        logger.info(
            "run_complete run=%s status=%s tokens=%d",
            run_id,
            complete_payload.get("status"),
            int(complete_payload.get("tokens_used", 0)),
        )
        return


async def _run_one(client: httpx.AsyncClient, claim: dict[str, Any]) -> None:
    from soctalk.graph.builder import build_secops_graph
    from soctalk.graph.event_sink import EVENT_SINK, RunEventSink

    run_id = str(claim["run_id"])
    lease_id = str(claim["lease_id"])

    state = _build_state(claim)
    graph = build_secops_graph()

    # Replay event sink (#72): set BEFORE ainvoke so graph nodes (same task
    # context) can emit; flushed alongside heartbeats and once at the end.
    sink = RunEventSink()
    sink_token = EVENT_SINK.set(sink)

    stop = asyncio.Event()
    hb = asyncio.create_task(
        _heartbeat_loop(client, run_id, lease_id, state, stop, sink),
        name=f"hb-{run_id[:8]}",
    )
    final: dict[str, Any] = {}
    last_error: str | None = None
    try:
        final = await graph.ainvoke(state, {"recursion_limit": 50})
    except Exception as e:  # noqa: BLE001
        last_error = str(e)[:4000]
        logger.exception("graph_invoke_failed run=%s", run_id)
    finally:
        stop.set()
        await hb
        EVENT_SINK.reset(sink_token)

    used = int(final.get("tokens_used", state.get("tokens_used", 0)))
    dollars_used = float(final.get("dollars_used", state.get("dollars_used", 0.0)))
    halted = bool(final.get("budget_terminated"))
    verdict_err = final.get("verdict_error") or {}
    verdict_err_category = verdict_err.get("category") if isinstance(verdict_err, dict) else None
    supervisor_err = final.get("supervisor_error") or {}
    supervisor_err_category = (
        supervisor_err.get("category") if isinstance(supervisor_err, dict) else None
    )
    # Transient backend unavailability (cold serverless endpoint, #77): release
    # the run for a bounded retry on the SAME run_id instead of failing it. The
    # server enforces the attempt cap and terminalizes once exhausted, so no
    # completed-run side effect ever replays. Checked FIRST, before the terminal
    # status mapping below, and only for the classified transient categories.
    transient_category = releasable_error_category(
        supervisor_err_category, verdict_err_category
    )
    if transient_category is not None and not halted:
        # Flush the tail of the replay journey before releasing (release clears
        # the lease, same as complete), then release; do NOT build a verdict.
        for attempt in range(3):
            if await _flush_replay_events(client, run_id, lease_id, sink):
                break
            await asyncio.sleep(min(2**attempt, 4))
        logger.info("run_transient run=%s category=%s -> release", run_id, transient_category)
        await _post_release(
            client, run_id, lease_id, transient_category, used, dollars_used,
            cost_basis=state.get("cost_basis"), price_source=state.get("price_source"),
        )
        return

    if last_error:
        status = "failed"
    elif halted:
        status = "halted_budget"
    elif supervisor_err_category:
        # Same contract as verdict_error below: a provider failure in the
        # supervisor must not masquerade as a completed triage.
        status = "failed"
        last_error = f"supervisor_failed:{supervisor_err_category}"
    elif verdict_err_category:
        # LLM provider failed mid-run — credit lack, rate limit, etc.
        # Mark the run failed so the API skips pending_reviews creation;
        # without this the verdict's empty/error state would be coerced
        # into a fake escalation and the raw provider error message
        # would leak into the user-facing review description.
        status = "failed"
        last_error = f"verdict_failed:{verdict_err_category}"
    else:
        status = "completed"

    # Failed runs MUST NOT carry a disposition or verdict summary —
    # those fields drive HIL row creation downstream.
    floor_vetoes: list[str] = []
    if status == "failed":
        disposition = None
        verdict_summary = None
    else:
        disposition = _disposition_from_final(final, status)
        # Terminal safety veto (issue #43): the floor sits on the auto-close
        # path itself, not on a single graph edge — even if every in-graph
        # guard is bypassed (supervisor CLOSE short-circuit, future graph
        # changes), a close over malicious signal cannot reach complete().
        if disposition == "close_fp":
            from soctalk.core.ir import replay_events
            from soctalk.triage_policy.floor import apply_worker_floor

            disposition, floor_vetoes = apply_worker_floor(final, disposition)
            sink.emit(
                replay_events.guard_evaluated(
                    stage="worker_floor",
                    decision_in="close_fp",
                    decision_out=disposition,
                    effect="pass" if not floor_vetoes else "override",
                    fired=floor_vetoes,
                )
            )
            if floor_vetoes:
                logger.warning(
                    "safety_floor_veto run=%s vetoes=%s close_fp->escalate",
                    run_id,
                    floor_vetoes,
                )
        verdict_summary = _verdict_summary(final)
    logger.info(
        "disposition_decided run=%s status=%s disposition=%s "
        "verdict_decision=%r supervisor_action=%r supervisor_conf=%r",
        run_id,
        status,
        disposition,
        (final.get("verdict") or {}).get("decision"),
        (final.get("supervisor_decision") or {}).get("next_action"),
        (final.get("supervisor_decision") or {}).get("tp_confidence"),
    )

    # Guard/floor audit trail rides the enrichments blob into pending_reviews so
    # the analyst sees the LLM draft, the guardrail that fired, and the final
    # disposition (issue #43: an audit event per override).
    enrichments_payload = _verdict_enrichments(final)
    # Canonical enrichment key is triage_policy_audit; also emit the deprecated
    # playbook_audit for one release so an older L1 reader still finds it.
    _tp_audit = final.get("triage_policy_audit") or final.get("playbook_audit")
    if _tp_audit:
        _tp_audit = list(_tp_audit)[:10]
        enrichments_payload["triage_policy_audit"] = _tp_audit
        enrichments_payload["playbook_audit"] = _tp_audit
    if floor_vetoes:
        enrichments_payload["safety_floor"] = {
            "vetoes": floor_vetoes,
            "overridden_from": "close_fp",
            "to": "escalate",
        }

    # ASK_AUTHORIZATION (epic M3): when the verdict is needs_more_info specifically because
    # authorization is absent (not contradicted, not malicious), attach a typed question so the
    # review UI can offer the analyst a one-click "save reusable authorization" answer. Keyed on
    # the RAW verdict decision (needs_more_info), not the effective escalate disposition. Rides
    # the same enrichments blob into pending_reviews; the detector reuses the canonical
    # derive_authz_class so it can never disagree with the floor about absent vs contradicted.
    if status == "completed":
        from soctalk.authorization.question import authorization_question_for

        _v = final.get("verdict") or {}
        _raw_decision = _v.get("decision")
        _raw_decision = str(getattr(_raw_decision, "value", _raw_decision) or "").lower()
        _question = authorization_question_for(
            final.get("investigation") or {}, disposition=_raw_decision
        )
        if _question is not None:
            enrichments_payload["authorization_question"] = _question.model_dump(mode="json")

    complete_payload = {
        "lease_id": lease_id,
        "status": status,
        "tokens_used": used,
        "dollars_used": dollars_used,
        "cost_basis": state.get("cost_basis"),
        "price_source": state.get("price_source"),
        "last_error": last_error,
        "disposition": disposition,
        "verdict_summary": verdict_summary,
        "verdict_confidence": _verdict_confidence(final),
        "findings": _verdict_findings(final),
        "enrichments": enrichments_payload,
    }
    # Final replay flush BEFORE complete: completion clears the lease, and
    # a post-completion flush would 409 and drop the tail of the journey.
    # Bounded retry — the tail carries the worker-floor and verdict beats.
    for attempt in range(3):
        if await _flush_replay_events(client, run_id, lease_id, sink):
            break
        await asyncio.sleep(min(2**attempt, 4))
    else:
        logger.warning("replay_final_flush_gave_up run=%s (tail beats lost)", run_id)
    await _post_complete(client, run_id, complete_payload)


async def _claim_one(client: httpx.AsyncClient) -> dict[str, Any] | None:
    token = _read_token()
    resp = await client.post(
        f"{_api_url()}/api/internal/worker/runs/claim",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Worker-Id": os.environ.get("HOSTNAME", "runs-worker"),
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    if resp.status_code == 200 and resp.text.strip() in ("", "null"):
        return None
    body = resp.json()
    if not body:
        return None
    # A structured denial is still "nothing to claim" as far as the loop is
    # concerned, but it says WHY (#129, #141 phase 2.2). Logged at info so an
    # operator watching worker logs sees "over the daily ceiling, resets in N
    # seconds" instead of silence indistinguishable from an idle queue.
    #
    # Checked before the body is treated as a run: a denial dict is truthy, so
    # without this it would be handled as a claim and fail downstream on a
    # missing run id.
    if isinstance(body, dict) and body.get("denied"):
        logger.info(
            "claim_denied reason=%s detail=%s retry_after=%ss",
            body.get("reason"),
            body.get("detail"),
            body.get("retry_after_seconds"),
        )
        return None
    return body


async def _worker_loop(
    client: httpx.AsyncClient,
    stop: asyncio.Event,
    idle_sleep: float,
    busy_sleep: float,
    slot: int,
) -> None:
    """One claim→run→complete loop. N of these run concurrently per process
    (issue #61) so multiple investigations are in flight at once, which is what
    fills a shared vLLM/SGLang backend's continuous batch. The claim endpoint is
    concurrency-safe (FOR UPDATE ... SKIP LOCKED), so N loops never double-claim.

    A single run's failure must never break the loop: it is caught here so the
    slot keeps pulling work. Idle waits carry per-slot jitter so N loops don't
    stampede the claim endpoint in lockstep when the queue is empty.
    """
    # Deterministic per-slot jitter (no RNG): stagger idle polls across slots.
    jitter = (slot % 8) * 0.25
    while not stop.is_set():
        try:
            claim = await _claim_one(client)
        except Exception as e:  # noqa: BLE001
            logger.warning("claim_failed slot=%d err=%s", slot, e)
            claim = None

        if claim is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_sleep + jitter)
            except TimeoutError:
                pass
            continue

        try:
            await _run_one(client, claim)
        except asyncio.CancelledError:
            # Drain deadline exceeded: propagate so the run is cancelled and the
            # server-side lease reaper requeues it. Do not swallow.
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "run_one_failed slot=%d run=%s err=%s",
                slot,
                str(claim.get("run_id")),
                e,
            )
        if busy_sleep > 0:
            await asyncio.sleep(busy_sleep)


async def main() -> int:
    idle_sleep = float(os.environ.get("WORKER_IDLE_SLEEP_SECONDS", "5"))
    busy_sleep = float(os.environ.get("WORKER_BUSY_SLEEP_SECONDS", "0"))
    concurrency = max(1, int(os.environ.get("WORKER_RUN_CONCURRENCY", "1")))
    drain_seconds = float(os.environ.get("WORKER_DRAIN_SECONDS", "60"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info(
        "runs_worker_start version=%s api=%s concurrency=%d",
        VERSION,
        _api_url(),
        concurrency,
    )

    # Bind MCP clients so cortex_worker_node / misp_worker_node /
    # thehive_worker_node have actual MCP clients to call during graph
    # execution. Without this, enrichment is a no-op and the verdict
    # LLM perpetually returns ``needs_more_info``. Bind is graceful —
    # if individual MCP server connections fail (chart components not
    # enabled), the worker logs and the graph node gets None and
    # skips. See codex round-1 finding: "runs_worker does not bind MCP
    # clients before graph execution".
    mcp_bound = False
    try:
        from soctalk.mcp.bindings import bind_clients
        from soctalk.settings_provider import (
            create_mcp_configs,
            load_integration_settings_from_env,
        )
        env_settings = load_integration_settings_from_env()
        mcp_configs = create_mcp_configs(env_settings)
        await bind_clients(mcp_configs)
        mcp_bound = True
    except Exception as e:  # noqa: BLE001
        logger.warning("mcp_bind_failed err=%s", e)

    # Pool sized to concurrency so N loops' claims/heartbeats/completes don't
    # serialize on the connection pool. Each run also spawns a heartbeat, so
    # budget roughly 2 connections per slot plus headroom.
    limits = httpx.Limits(
        max_connections=max(10, concurrency * 2 + 4),
        max_keepalive_connections=max(10, concurrency * 2 + 4),
    )
    async with httpx.AsyncClient(limits=limits) as client:
        loops = [
            asyncio.create_task(
                _worker_loop(client, stop, idle_sleep, busy_sleep, slot),
                name=f"worker-loop-{slot}",
            )
            for slot in range(concurrency)
        ]
        # Loops exit on their own once ``stop`` is set and their current run
        # finishes — that is the graceful drain. Bound it: if a run outlives the
        # drain deadline, cancel the stragglers and let lease expiry requeue.
        await stop.wait()
        logger.info("runs_worker_draining concurrency=%d timeout=%.0fs", concurrency, drain_seconds)
        _done, pending = await asyncio.wait(loops, timeout=drain_seconds)
        if pending:
            logger.warning(
                "runs_worker_drain_timeout cancelling=%d "
                "(in-flight runs will be requeued by lease expiry)",
                len(pending),
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    if mcp_bound:
        try:
            from soctalk.mcp.bindings import cleanup_clients
            await cleanup_clients()
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp_cleanup_failed err=%s", e)
    logger.info("runs_worker_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
