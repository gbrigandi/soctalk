"""SocTalk per-tenant adapter — heartbeat + Wazuh alert ingest to L1."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI

from soctalk_wire import (
    REDACTION_VERSION,
    SCHEMA_VERSION,
    TEMPLATE_VERSION,
    redact_text,
    template_hash,
)

logger = logging.getLogger("soctalk.adapter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

VERSION = "0.2.1"

# Startup durable-checkpoint load: retry with a freshly read token so a token
# renewed just after pod start (see token_renewal) is picked up before we
# ingest from a stale local cursor. ~60s total covers Secret projection; a
# no-checkpoint tenant returns 200 (empty) and loads on the first try.
CHECKPOINT_LOAD_MAX_ATTEMPTS = 10
CHECKPOINT_LOAD_RETRY_SECONDS = 6.0


def _read_token() -> str:
    path = Path(os.environ.get("ADAPTER_TOKEN_PATH", "/run/secrets/adapter/token"))
    return path.read_text().strip()


def _initial_alert_ts() -> str:
    """Initial cursor for alert ingestion.

    Defaults to epoch (full backfill). Set SOCTALK_INGEST_INITIAL_TS to
    an ISO-8601 timestamp to start ingesting from a specific point, or
    to the literal string ``"now"`` to skip backfill entirely (start
    from the moment the adapter boots).
    """
    raw = os.environ.get("SOCTALK_INGEST_INITIAL_TS", "").strip()
    if not raw:
        return "1970-01-01T00:00:00.000Z"
    if raw.lower() == "now":
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"
    return raw


class _State:
    def __init__(self) -> None:
        self.last_heartbeat_ok: datetime | None = None
        self.last_heartbeat_error: str | None = None
        self.last_alert_ts: str = _initial_alert_ts()
        # search_after tie-breaker: the ``id`` of the last event ingested at
        # last_alert_ts. None means "start of this timestamp".
        self.last_alert_id: str | None = None
        self.alerts_queried: int = 0
        self.alerts_forwarded: int = 0
        self.alerts_duplicate: int = 0
        self.alerts_dropped_rate_limit: int = 0
        self.batch_seq: int = 0
        self.checkpoint_loaded: bool = False
        self.last_ingest_error: str | None = None


_state = _State()


class _TokenBucket:
    """Cooperative rate-limiter for per-tenant alert ingestion.

    The adapter is single-tenant (one process per tenant), so a process-
    local token bucket is the per-tenant cap by construction. ``rate``
    is alerts/sec, ``burst`` is the bucket size. Excess alerts are
    dropped (and counted on ``_state.alerts_dropped_rate_limit``) rather
    than queued — better to lose a tail of a flood than to lag forever.
    """

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = max(rate_per_sec, 0.0)
        self.burst = max(burst, 1)
        self.tokens = float(self.burst)
        self.last = time.monotonic()

    def take(self, n: int) -> tuple[int, int]:
        """Take up to ``n`` tokens. Returns (allowed, dropped)."""
        if self.rate <= 0:
            return n, 0  # rate-limiter disabled
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        allowed = min(n, int(self.tokens))
        self.tokens -= allowed
        return allowed, n - allowed


def _make_rate_limiter() -> _TokenBucket:
    per_min = float(os.environ.get("SOCTALK_ADAPTER_RATE_LIMIT_PER_MIN", "60"))
    burst = int(os.environ.get("SOCTALK_ADAPTER_RATE_LIMIT_BURST", "30"))
    return _TokenBucket(rate_per_sec=per_min / 60.0, burst=burst)


_rate_limiter = _make_rate_limiter()


def _wazuh_indexer_url() -> str:
    return os.environ.get(
        "WAZUH_INDEXER_URL", "https://wazuh-indexer:9200"
    ).rstrip("/")


def _wazuh_indexer_creds() -> tuple[str, str]:
    return (
        os.environ.get("WAZUH_INDEXER_USERNAME", "admin"),
        os.environ.get("WAZUH_INDEXER_PASSWORD", "admin"),
    )


def _wazuh_indexer_verify_ssl() -> bool:
    """Resolve TLS verification for the Wazuh indexer httpx client.

    Reads ``WAZUH_INDEXER_VERIFY_SSL`` (default ``"true"``). Recognises the
    canonical spellings ``true``/``1`` (verify ON) and ``false``/``0`` (verify
    OFF), case-insensitive and whitespace-trimmed. Any other value is
    malformed: log a warning and fail safe to verification ON — a typo must
    never silently disable TLS verification against the indexer. The chart
    feeds this from ``IntegrationConfig.wazuh_verify_ssl`` so a tenant whose
    external (or in-cluster self-signed) indexer needs ``verify=False`` can
    opt out explicitly.
    """
    raw = os.environ.get("WAZUH_INDEXER_VERIFY_SSL", "true")
    normalized = raw.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    logger.warning(
        "WAZUH_INDEXER_VERIFY_SSL=%r is not a recognised boolean; "
        "defaulting to verify=True",
        raw,
    )
    return True


def _soctalk_api_verify_ssl() -> bool:
    """Resolve TLS verification for the SocTalk L1 (MSSP) API httpx clients.

    Reads ``SOCTALK_API_VERIFY_SSL`` (default ``"true"``) with the same
    spelling rules as ``WAZUH_INDEXER_VERIFY_SSL``. The provisioning controller
    sets this to ``"false"`` for cross-cluster tenants whose L1 serves a
    self-signed cert (launchpad demo / pending launchpad-owned certs): the
    adapter must reach L1 to heartbeat and forward alerts, so a self-signed L1
    can opt out of verification explicitly. A malformed value fails safe to
    verify=True — a typo must never silently disable TLS against L1.
    """
    raw = os.environ.get("SOCTALK_API_VERIFY_SSL", "true")
    normalized = raw.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    logger.warning(
        "SOCTALK_API_VERIFY_SSL=%r is not a recognised boolean; "
        "defaulting to verify=True",
        raw,
    )
    return True


def _severity_from_rule_level(level: int | None) -> int:
    if level is None:
        return 0
    return max(0, min(15, int(level)))


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SHA256_RE = re.compile(r"\b[a-fA-F0-9]{64}\b")
_MD5_RE = re.compile(r"\b[a-fA-F0-9]{32}\b")
_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ru|cn|tk|xyz|info|biz)\b",
    re.IGNORECASE,
)


def _is_routable_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b, *_ = (int(p) for p in parts)
    except ValueError:
        return False
    if a in (10, 127, 0):
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 169 and b == 254:
        return False
    return True


def _extract_iocs(text: str) -> list[dict]:
    if not text:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for ip in _IPV4_RE.findall(text):
        if not _is_routable_ip(ip):
            continue
        key = ("ip", ip)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": "ip", "value": ip})
    for h in _SHA256_RE.findall(text):
        key = ("hash_sha256", h.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": "hash_sha256", "value": h.lower()})
    for h in _MD5_RE.findall(text):
        if any(k[1] == h.lower() for k in seen):
            continue
        key = ("hash_md5", h.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": "hash_md5", "value": h.lower()})
    for d in _DOMAIN_RE.findall(text):
        key = ("domain", d.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": "domain", "value": d.lower()})
    return out[:32]


_NAME_KV_RE = re.compile(r"\bname=([A-Za-z0-9_\-.]+)")
_FIM_FILE_RE = re.compile(r"File '([^']+)' (?:was )?(?:modified|added|deleted|changed)", re.I)
_USERID_KV_RE = re.compile(r"\b(?:USER|user|uid)=([A-Za-z0-9_\-.]+)")
_IP_RE = re.compile(r"\b(?:from|src ip|source)\s*[=:]?\s*((?:\d{1,3}\.){3}\d{1,3})", re.I)


def _extract_subject(full_log: str) -> str | None:
    """Best-effort extraction of the alert's primary subject from
    Wazuh's ``full_log`` line. Handles common useradd / groupadd / FIM
    / authentication patterns. Returns ``None`` if no obvious subject
    found — the title falls back to rule description only.
    """
    if not full_log:
        return None
    for rx in (_FIM_FILE_RE, _NAME_KV_RE, _USERID_KV_RE, _IP_RE):
        m = rx.search(full_log)
        if m:
            return m.group(1)[:80]
    return None


def _compose_title(rule_desc: str, agent_name: str | None, subject: str | None) -> str:
    """Compose an analyst-friendly title: ``{rule_desc}[: subject][ on agent]``.

    Examples:
      - "New user added: attacker_test on linux-ep-0"
      - "Integrity checksum changed: /etc/passwd on linux-ep-0"
      - "Authentication failure on linux-ep-0"
    """
    base = (rule_desc or "Wazuh alert").strip().rstrip(".")
    if subject:
        base = f"{base}: {subject}"
    if agent_name:
        base = f"{base} on {agent_name}"
    return base[:255]


def _extract_entities(src: dict, agent: dict, agent_name: str | None) -> list[dict]:
    """Typed, role-carrying entities from fields the Wazuh decoder already
    parsed (issue #17 fix 1). ``source_field`` preserves provenance.

    Wazuh puts decoded fields under ``data`` (data.srcuser, data.srcip,
    data.dstuser, data.win.eventdata.*). We map the common ones; unknown
    shapes are simply not emitted rather than guessed.
    """
    ents: list[dict] = []

    def add(t: str, v, role: str | None, field: str) -> None:
        if v is None:
            return
        s = str(v).strip()
        if s:
            ents.append({"type": t, "value": s[:512], "role": role, "source_field": field})

    if isinstance(agent, dict) and agent.get("id"):
        add("host", agent.get("name") or agent.get("id"), "target", "agent.name")
    data = src.get("data") or {}
    if isinstance(data, dict):
        add("user", data.get("srcuser"), "actor", "data.srcuser")
        add("user", data.get("dstuser"), "target", "data.dstuser")
        add("user", data.get("user"), "actor", "data.user")
        add("ip", data.get("srcip"), "src", "data.srcip")
        add("ip", data.get("dstip"), "dst", "data.dstip")
        add("port", data.get("srcport"), "src", "data.srcport")
        add("port", data.get("dstport"), "dst", "data.dstport")
        add("process", data.get("process") or data.get("command"), "actor", "data.process")
    # dedupe on (type, value, role)
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in ents:
        k = (e["type"], e["value"], e["role"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out[:64]


def _extract_mitre(rule: dict) -> dict:
    """Pull MITRE ATT&CK refs from the rule (issue #17 fix 2)."""
    mitre = rule.get("mitre") or {}
    if not isinstance(mitre, dict):
        return {}
    def _cap(v):
        return [str(x)[:32] for x in (v or [])][:16]
    out = {
        "ids": _cap(mitre.get("id")),
        "tactics": _cap(mitre.get("tactic")),
        "techniques": _cap(mitre.get("technique")),
    }
    return out if any(out.values()) else {}


def _hit_to_event(hit: dict) -> dict | None:
    src = hit.get("_source") or {}
    source_id = src.get("id") or hit.get("_id")
    if not source_id:
        return None
    rule = src.get("rule") or {}
    agent = src.get("agent") or {}
    full_log = src.get("full_log") or ""
    rule_desc = rule.get("description") or ""
    agent_name = agent.get("name") if isinstance(agent, dict) else None
    asset_ids: list[str] = []
    if isinstance(agent, dict) and agent.get("id"):
        asset_ids.append(agent["id"][:64])
    if agent_name:
        asset_ids.append(agent_name[:64])

    # IOC extraction reads the RAW text (must run before redaction).
    iocs = _extract_iocs(f"{rule_desc} {full_log}")
    entities = _extract_entities(src, agent, agent_name)

    # Redaction (issue #17 fix 9): strip secrets from every outbound text
    # path AFTER IOC extraction, BEFORE anything leaves the tenant. Redact
    # on the FULL text, THEN truncate — truncating first could cut a
    # multi-line secret (e.g. a PEM block) before its END marker so the
    # pattern never matches.
    full_log_red = redact_text(full_log)[:4096] if full_log else ""
    rule_desc_red = redact_text(rule_desc)[:512] if rule_desc else ""
    description = redact_text((full_log or rule_desc).strip())[:1024] or None
    title = redact_text(
        _compose_title(rule_desc, agent_name, _extract_subject(full_log))
    )
    # Template hash over REDACTED text: masking secrets out of the hash
    # keeps the fingerprint secret-free AND stable when only a secret varies.
    thash = template_hash(full_log_red)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {
        "source_event_id": str(source_id)[:128],
        "source": "wazuh",
        "rule_id": (str(rule.get("id"))[:64] if rule.get("id") else None),
        "severity": _severity_from_rule_level(rule.get("level")),
        "asset_ids": asset_ids[:8],
        "initial_iocs": iocs,
        "ts": src.get("@timestamp") or src.get("timestamp"),
        "observed_at": now_iso,
        "description": description,
        "title": title,
        # v2 evidence sidecar
        "entities": entities,
        "mitre": _extract_mitre(rule),
        "rule_groups": [str(g)[:64] for g in (rule.get("groups") or [])][:16],
        "decoder": (src.get("decoder") or {}).get("name"),
        "full_log": full_log_red,
        "template_hash": thash,
        "template_version": TEMPLATE_VERSION,
        "redaction_version": REDACTION_VERSION,
        "raw": {
            "rule_description": rule_desc_red,
            "rule_groups": rule.get("groups") or [],
            "decoder_name": (src.get("decoder") or {}).get("name"),
            "location": src.get("location"),
            "manager_name": (src.get("manager") or {}).get("name"),
            "full_log": full_log_red,
        },
    }


def _min_severity() -> int:
    raw = os.environ.get("SOCTALK_ADAPTER_MIN_SEVERITY", "10")
    try:
        v = int(raw)
    except ValueError:
        return 10
    return max(0, min(15, v))


async def _query_alerts(
    client: httpx.AsyncClient, since_ts: str, since_id: str | None, limit: int
) -> list[dict]:
    user, pw = _wazuh_indexer_creds()
    # Keyset pagination (issue #17 fix 6): sort by (@timestamp, id) and use
    # ``search_after`` so a page that ends mid-timestamp resumes exactly
    # after the last event — no skipping same-timestamp events, and no
    # livelock when more than ``limit`` alerts share one timestamp. Re-reads
    # are still absorbed by the control-plane idempotency constraint.
    filters: list[dict] = [
        {"range": {"@timestamp": {"gte": since_ts}}},
        {"range": {"rule.level": {"gte": _min_severity()}}},
    ]
    must_not: list[dict] = []
    # By default the Wazuh manager pod's agent (id 000) flood-generates
    # FIM/monitord self-alerts (rules 510/550/553/...) that aren't
    # security signals. Default behaviour is to skip them; flip the
    # env var to "0" to ingest manager-self alerts too.
    if os.environ.get("SOCTALK_ADAPTER_EXCLUDE_MANAGER_AGENT", "1") in {"1", "true"}:
        must_not.append({"term": {"agent.id": "000"}})
    # Optional allowlist by agent.name prefix — when set, only agents
    # whose name starts with the prefix are ingested. Useful to scope
    # ingestion to docker-based endpoints like ``linux-ep-*``.
    prefix = os.environ.get("SOCTALK_ADAPTER_AGENT_PREFIX")
    if prefix:
        filters.append({"prefix": {"agent.name": prefix}})
    bool_query: dict = {"filter": filters}
    if must_not:
        bool_query["must_not"] = must_not
    body: dict = {
        "size": limit,
        # id.keyword is the stable secondary sort so ``search_after`` gives a
        # total order across equal timestamps.
        "sort": [{"@timestamp": {"order": "asc"}}, {"id": {"order": "asc"}}],
        "query": {"bool": bool_query},
    }
    if since_id is not None:
        body["search_after"] = [since_ts, since_id]
    resp = await client.post(
        f"{_wazuh_indexer_url()}/wazuh-alerts-*/_search",
        auth=(user, pw),
        json=body,
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return list(data.get("hits", {}).get("hits", []))


async def _load_checkpoint(
    client: httpx.AsyncClient, api_url: str, tenant_id: str, token: str
) -> None:
    try:
        resp = await client.get(
            f"{api_url}/api/internal/adapter/checkpoint?source=wazuh",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        cp = resp.json()
        if cp.get("cursor_ts"):
            _state.last_alert_ts = cp["cursor_ts"]
        _state.last_alert_id = cp.get("cursor_event_id")
        _state.batch_seq = int(cp.get("batch_seq") or 0)
        _state.checkpoint_loaded = True
        logger.info("checkpoint_loaded cursor=%s id=%s batch_seq=%d",
                    _state.last_alert_ts, _state.last_alert_id, _state.batch_seq)
    except Exception as e:  # noqa: BLE001
        logger.warning("checkpoint_load_failed: %s (starting from local cursor)", e)


async def _save_checkpoint(
    client: httpx.AsyncClient, api_url: str, tenant_id: str, token: str,
    cursor_ts: str, cursor_event_id: str | None,
) -> None:
    try:
        resp = await client.put(
            f"{api_url}/api/internal/adapter/checkpoint",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "tenant_id": tenant_id,
                "source": "wazuh",
                "cursor_ts": cursor_ts,
                "cursor_event_id": cursor_event_id,
                "batch_seq": _state.batch_seq,
                "dropped_total": _state.alerts_dropped_rate_limit,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.warning("checkpoint_save_failed: %s", e)


async def _heartbeat_once(client: httpx.AsyncClient) -> None:
    api_url = os.environ["SOCTALK_API_URL"].rstrip("/")
    tenant_id = os.environ["SOCTALK_TENANT_ID"]
    token = _read_token()
    # Coverage metrics on the heartbeat (issue #17 fix 10, Alertmanager-style
    # loss accounting): the control plane can tell a quiet tenant from a
    # dropping one, and see ingest throughput without a separate channel.
    metrics = {
        "alerts_queried": _state.alerts_queried,
        "alerts_forwarded": _state.alerts_forwarded,
        "alerts_duplicate": _state.alerts_duplicate,
        "alerts_dropped_rate_limit": _state.alerts_dropped_rate_limit,
        "batch_seq": _state.batch_seq,
        "last_alert_ts": _state.last_alert_ts,
        "last_ingest_error": _state.last_ingest_error,
    }
    resp = await client.post(
        f"{api_url}/api/internal/adapter/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"tenant_id": tenant_id, "version": VERSION, "health": "ok",
              "metrics": metrics},
        timeout=10.0,
    )
    resp.raise_for_status()


async def _heartbeat_loop() -> None:
    interval = float(os.environ.get("SOCTALK_HEARTBEAT_INTERVAL_SECONDS", "30"))
    async with httpx.AsyncClient(verify=_soctalk_api_verify_ssl()) as client:
        while True:
            try:
                await _heartbeat_once(client)
                _state.last_heartbeat_ok = datetime.now(timezone.utc)
                _state.last_heartbeat_error = None
                logger.info("heartbeat_ok")
            except Exception as e:  # noqa: BLE001
                _state.last_heartbeat_error = str(e)
                logger.warning("heartbeat_failed: %s", e)
            await asyncio.sleep(interval)


async def _ingest_loop() -> None:
    if os.environ.get("SOCTALK_INGEST_DISABLED", "0") in {"1", "true"}:
        logger.info("ingest_disabled")
        return
    interval = float(os.environ.get("SOCTALK_INGEST_INTERVAL_SECONDS", "15"))
    batch_size = int(os.environ.get("SOCTALK_INGEST_BATCH_SIZE", "100"))
    api_url = os.environ["SOCTALK_API_URL"].rstrip("/")
    tenant_id = os.environ["SOCTALK_TENANT_ID"]
    token = _read_token()

    # TLS verification against the indexer is tenant-controlled via
    # WAZUH_INDEXER_VERIFY_SSL (default on); resolved once here instead of the
    # former hard-coded verify=False so externally-provided CA-signed indexers
    # are verified while self-signed in-cluster ones can opt out.
    verify_indexer_tls = _wazuh_indexer_verify_ssl()
    verify_api_tls = _soctalk_api_verify_ssl()
    async with (
        httpx.AsyncClient(verify=verify_api_tls) as api_client,
        httpx.AsyncClient(verify=verify_indexer_tls) as wazuh_client,
    ):
        # Durable checkpoint resume (issue #17 fix 6): pull the server-side
        # cursor once at start so a pod restart continues instead of
        # replaying from the in-memory initial cursor.
        # Load the durable cursor BEFORE ingesting, retrying with a freshly
        # read token each attempt. If the pod started with an about-to-expire
        # token that the control plane then renewed, the first load 401s;
        # proceeding from the local (epoch/now) cursor would replay weeks of
        # alerts or skip history. Retry so we resume from the true checkpoint
        # once the renewed token projects into the mounted Secret (~60s).
        for _attempt in range(CHECKPOINT_LOAD_MAX_ATTEMPTS):
            await _load_checkpoint(api_client, api_url, tenant_id, _read_token())
            if _state.checkpoint_loaded:
                break
            await asyncio.sleep(CHECKPOINT_LOAD_RETRY_SECONDS)
        else:
            logger.warning(
                "checkpoint_never_loaded attempts=%d — starting from local cursor",
                CHECKPOINT_LOAD_MAX_ATTEMPTS,
            )
        while True:
            # Re-read the token each cycle so a renewed ``adapter-token`` Secret
            # (the control plane re-mints it before the TTL elapses) is picked
            # up without a pod restart — the mounted file updates in place.
            token = _read_token()
            try:
                hits = await _query_alerts(
                    wazuh_client, _state.last_alert_ts, _state.last_alert_id, batch_size
                )
                _state.alerts_queried += len(hits)
                if hits:
                    events: list[dict] = []
                    for h in hits:
                        ev = _hit_to_event(h)
                        if ev is not None:
                            events.append(ev)
                    # Keyset cursor: the resume point is the LAST hit's
                    # (ts, id) regardless of whether the timestamp advanced.
                    # This is what breaks the same-timestamp livelock — the
                    # id tie-breaker always moves forward even when many
                    # alerts share one timestamp.
                    new_cursor_ts = _state.last_alert_ts
                    new_cursor_id = _state.last_alert_id
                    if events:
                        last = events[-1]
                        new_cursor_ts = last["ts"] or new_cursor_ts
                        new_cursor_id = last["source_event_id"]

                    # Per-tenant rate limit — drop the tail past the bucket
                    # capacity. The cursor still advances past dropped events
                    # (shedding, not deferral); drops are recorded as facts.
                    if events:
                        allowed, dropped = _rate_limiter.take(len(events))
                        if dropped > 0:
                            _state.alerts_dropped_rate_limit += dropped
                            logger.warning(
                                "rate_limited dropped=%d total_dropped=%d batch=%d",
                                dropped, _state.alerts_dropped_rate_limit, len(events),
                            )
                        events = events[:allowed]

                    if events:
                        _state.batch_seq += 1
                        resp = await api_client.post(
                            f"{api_url}/api/internal/adapter/events",
                            headers={"Authorization": f"Bearer {token}"},
                            json={
                                "tenant_id": tenant_id,
                                "events": events,
                                "schema_version": SCHEMA_VERSION,
                                "batch_seq": _state.batch_seq,
                            },
                            timeout=30.0,
                        )
                        resp.raise_for_status()
                        body = resp.json()
                        dup = (body.get("action_counts") or {}).get("duplicate", 0)
                        _state.alerts_duplicate += dup
                        _state.alerts_forwarded += len(events) - dup
                        _state.last_ingest_error = None

                    # Advance + persist the keyset cursor whether we forwarded
                    # or shed the whole batch — either way those hits are done.
                    if (new_cursor_ts, new_cursor_id) != (
                        _state.last_alert_ts, _state.last_alert_id
                    ):
                        _state.last_alert_ts = new_cursor_ts
                        _state.last_alert_id = new_cursor_id
                        await _save_checkpoint(
                            api_client, api_url, tenant_id, token,
                            new_cursor_ts, new_cursor_id,
                        )
                        logger.info(
                            "ingest_ok forwarded=%d duplicate=%d total=%d cursor=%s/%s",
                            _state.alerts_forwarded, _state.alerts_duplicate,
                            _state.alerts_forwarded, new_cursor_ts, new_cursor_id,
                        )
            except Exception as e:
                _state.last_ingest_error = str(e)
                logger.warning("ingest_failed: %s", e)
            await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    del app
    hb = asyncio.create_task(_heartbeat_loop(), name="adapter-heartbeat")
    ig = asyncio.create_task(_ingest_loop(), name="adapter-ingest")
    try:
        yield
    finally:
        for t in (hb, ig):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


app = FastAPI(lifespan=_lifespan)


@app.get("/health/live")
async def live() -> dict:
    return {"ok": True, "version": VERSION}


@app.get("/health/ready")
async def ready() -> dict:
    # Ready as soon as the server is up; heartbeat + ingest status are
    # informational. The chart's readiness probe just needs the process
    # to be serving HTTP.
    return {
        "ok": True,
        "last_heartbeat_ok": _state.last_heartbeat_ok.isoformat()
        if _state.last_heartbeat_ok
        else None,
        "last_heartbeat_error": _state.last_heartbeat_error,
        "alerts_queried": _state.alerts_queried,
        "alerts_forwarded": _state.alerts_forwarded,
        "alerts_duplicate": _state.alerts_duplicate,
        "alerts_dropped_rate_limit": _state.alerts_dropped_rate_limit,
        "batch_seq": _state.batch_seq,
        "checkpoint_loaded": _state.checkpoint_loaded,
        "last_alert_ts": _state.last_alert_ts,
        "last_ingest_error": _state.last_ingest_error,
    }
