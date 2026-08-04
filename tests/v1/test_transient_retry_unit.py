"""Transient serverless-retry classification + worker release transport (#77).

Phase 1 of the RunPod-style e2e: a scale-to-zero backend that is cold-starting
(404 / no-workers / initializing) must be treated as a TRANSIENT failure the
worker releases-and-retries, NOT a terminal run failure. The two correctness
properties pinned here:

  * profile scoping: a cold-start signature becomes ``serverless_unavailable``
    ONLY when the resolved DeliveryProfile is scale_to_zero. The identical
    error on a warm frontier stays its original (terminal) error.
  * classification: the new error maps to the stable ``serverless_unavailable``
    category the worker branches on.

The endpoint semantics (attempts, not_before backoff, cap->failed, same
run_id) are covered by the DB-integration test.
"""

from __future__ import annotations

import types

import pytest

from soctalk.inference import (
    InferenceAccounting,
    InferenceRequest,
    InferenceTier,
    _is_cold_start_error,
    ainvoke_request,
)
from soctalk.llm import (
    LLMProviderError,
    SchemaValidationError,
    ServerlessUnavailableError,
    classify_llm_error,
)


class _Err404(Exception):
    """A provider error carrying an HTTP 404, like a cold RunPod proxy."""

    status_code = 404


class _Err429(Exception):
    """A provider error carrying an HTTP 429 (rate limit)."""

    status_code = 429


class _Err500(Exception):
    """A provider error carrying an HTTP 500."""

    status_code = 500


def test_classify_maps_serverless_unavailable():
    assert classify_llm_error(ServerlessUnavailableError("no workers")) == "serverless_unavailable"
    # Ordering: the serverless bucket wins over the generic provider bucket.
    assert classify_llm_error(SchemaValidationError("bad json")) == "schema_validation"
    assert classify_llm_error(_Err404("not found")) == "provider_error"


@pytest.mark.parametrize(
    "err,expected",
    [
        (_Err404("no workers available"), True),      # 404 + marker -> cold
        (Exception("Endpoint is starting, please try again"), True),  # marker match
        (Exception("503 Service Unavailable"), True),
        (_Err500("still loading weights"), True),      # 5xx status-only -> cold
        (Exception("connection refused"), True),
        (Exception("some genuinely bad request about a field"), False),
        (SchemaValidationError("schema broke"), False),
        # A bare 404/429 with NO cold-start marker is a permanent/rate error,
        # NOT a warming endpoint — must not be masked as transient (Codex #77).
        (_Err404("model 'nope' does not exist"), False),
        (_Err429("rate limit exceeded"), False),
    ],
)
def test_is_cold_start_error(err, expected):
    assert _is_cold_start_error(err) is expected


def _req() -> InferenceRequest:
    return InferenceRequest(
        tier=InferenceTier.ROUTER,
        metadata=InferenceAccounting(producer="test", budget_state=None),
    )


def _patch_backend(monkeypatch, *, readiness: str, raises: Exception, kind=None):
    """Route ainvoke_request through a fake backend that raises `raises`, with a
    resolved profile whose readiness we control. Isolates the wrap logic from
    real provider resolution. `kind` defaults to OPENAI_COMPAT so only tests
    that opt into RUNPOD_JOB exercise the health-probe branch."""
    import soctalk.inference as inf

    profile = types.SimpleNamespace(
        readiness=readiness, backend_id="test:model",
        kind=kind if kind is not None else inf.BackendKind.OPENAI_COMPAT,
    )
    resolved = types.SimpleNamespace(
        engine=None, provider="openai", decoding_mode=inf.DecodingMode.AUTO, model="m",
    )
    rb = types.SimpleNamespace(resolved=resolved, profile=profile)

    class _FakeBackend:
        async def invoke(self, req, resolved_, mode):  # noqa: ANN001
            raise raises

    monkeypatch.setattr(inf, "resolve_backend", lambda cfg, tier, model_override=None: rb)
    monkeypatch.setattr(inf, "select_backend", lambda rb_: _FakeBackend())
    monkeypatch.setattr(
        inf, "resolve_decoding_mode",
        lambda *a, **k: inf.DecodingMode.AUTO,
    )


async def test_cold_start_reclassified_only_for_scale_to_zero(monkeypatch):
    # scale_to_zero: a 404 becomes the transient serverless category.
    _patch_backend(monkeypatch, readiness="scale_to_zero", raises=_Err404("no workers"))
    with pytest.raises(ServerlessUnavailableError):
        await ainvoke_request(_req(), cfg=object())


async def test_cold_start_signature_on_warm_backend_stays_terminal(monkeypatch):
    # warm frontier: the SAME 404 is a real error (bad url/model), NOT transient.
    _patch_backend(monkeypatch, readiness="warm", raises=_Err404("not found"))
    with pytest.raises(_Err404):
        await ainvoke_request(_req(), cfg=object())


async def test_non_cold_start_error_passes_through_on_scale_to_zero(monkeypatch):
    # A scale_to_zero backend that returns a genuinely bad-request error (no
    # cold-start signature) must NOT be masked as transient.
    boom = LLMProviderError("model 'nope' does not exist")
    _patch_backend(monkeypatch, readiness="scale_to_zero", raises=boom)
    with pytest.raises(LLMProviderError):
        await ainvoke_request(_req(), cfg=object())


# ------------------------------------------- RunPod health verdict (issue #77)
# For RunPod serverless the primary cold/warm signal is the gateway's /health
# worker counts, not the error's status code or prose. These tests pin the
# three-way contract: warming wins over a status the heuristic calls terminal,
# serving wins over a status the heuristic calls transient, and a failed probe
# falls back to the heuristic instead of deciding.


def _patch_health(monkeypatch, verdict):
    import soctalk.inference as inf

    async def _fake_verdict(resolved):  # noqa: ANN001
        return verdict

    monkeypatch.setattr(inf, "_runpod_health_verdict", _fake_verdict)


async def test_runpod_bare_404_with_no_workers_is_transient(monkeypatch):
    # THE disputed case on #77: a marker-free 404 during a genuine cold start.
    # The prose heuristic calls it terminal; the health probe says nothing can
    # serve, so it is released for retry. An alert is never dropped because a
    # vendor phrased an error unhelpfully.
    import soctalk.inference as inf

    _patch_backend(monkeypatch, readiness="scale_to_zero",
                   raises=_Err404("{'detail': 'Not Found'}"),
                   kind=inf.BackendKind.RUNPOD_JOB)
    _patch_health(monkeypatch, True)
    with pytest.raises(ServerlessUnavailableError):
        await ainvoke_request(_req(), cfg=object())


async def test_runpod_cold_looking_500_with_ready_workers_is_terminal(monkeypatch):
    # The mirror image: a 500 the heuristic would release, but workers ARE
    # ready, so the gateway had capacity and the error is real. Without the
    # probe this would burn the whole retry budget on every alert against a
    # genuinely broken endpoint.
    import soctalk.inference as inf

    _patch_backend(monkeypatch, readiness="scale_to_zero",
                   raises=_Err500("internal server error"),
                   kind=inf.BackendKind.RUNPOD_JOB)
    _patch_health(monkeypatch, False)
    with pytest.raises(_Err500):
        await ainvoke_request(_req(), cfg=object())


async def test_runpod_probe_failure_falls_back_to_markers(monkeypatch):
    # Probe returns None (network blip, bad key): the heuristic decides, same
    # as before the probe existed. A dead probe must never mask the real error
    # in either direction.
    import soctalk.inference as inf

    _patch_backend(monkeypatch, readiness="scale_to_zero",
                   raises=_Err500("internal server error"),
                   kind=inf.BackendKind.RUNPOD_JOB)
    _patch_health(monkeypatch, None)
    with pytest.raises(ServerlessUnavailableError):
        await ainvoke_request(_req(), cfg=object())

    _patch_backend(monkeypatch, readiness="scale_to_zero",
                   raises=_Err404("model 'nope' does not exist"),
                   kind=inf.BackendKind.RUNPOD_JOB)
    with pytest.raises(_Err404):
        await ainvoke_request(_req(), cfg=object())


def test_runpod_endpoint_id_parsing():
    from soctalk.inference import _runpod_endpoint_id

    assert _runpod_endpoint_id("https://api.runpod.ai/v2/abc123/openai/v1") == "abc123"
    assert _runpod_endpoint_id("https://api.runpod.ai/v2/abc123") == "abc123"
    # A pod behind the proxy, a plain OpenAI-compatible host, and junk all
    # decline the probe rather than guessing.
    assert _runpod_endpoint_id("https://xyz-8000.proxy.runpod.net/v1") is None
    assert _runpod_endpoint_id("https://api.openai.com/v1") is None
    assert _runpod_endpoint_id("") is None
    assert _runpod_endpoint_id("https://api.runpod.ai/") is None


# ---- worker release transport tolerance ----

class _Resp:
    def __init__(self, status_code, text="{}"):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


class _Client:
    def __init__(self, results):
        self.calls = []
        self._results = list(results)

    async def post(self, url, **kwargs):  # noqa: ANN003
        self.calls.append(url)
        item = self._results.pop(0) if self._results else _Resp(200, '{"retrying": true}')
        if isinstance(item, Exception):
            raise item
        return item


async def test_post_release_409_is_benign(monkeypatch):
    from soctalk.runs_worker import main as w
    monkeypatch.setattr(w, "_read_token", lambda: "t")
    monkeypatch.setattr(w, "_api_url", lambda: "http://api")
    c = _Client([_Resp(409, "lease expired")])
    await w._post_release(c, "run-1", "lease-1", "serverless_unavailable", 0, 0.0)
    assert len(c.calls) == 1 and c.calls[0].endswith("/release")


async def test_post_release_retries_transport_then_succeeds(monkeypatch):
    from soctalk.runs_worker import main as w
    monkeypatch.setattr(w, "_read_token", lambda: "t")
    monkeypatch.setattr(w, "_api_url", lambda: "http://api")
    monkeypatch.setattr(w.asyncio, "sleep", lambda *_: _noop())
    c = _Client([RuntimeError("conn reset"), _Resp(200, '{"retrying": true, "attempts": 1}')])
    await w._post_release(c, "run-1", "lease-1", "serverless_unavailable", 5, 0.0)
    assert len(c.calls) == 2


async def _noop():
    return None
