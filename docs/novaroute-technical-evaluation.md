# NovaRoute AI: technical evaluation and SocTalk compatibility

Measured 2026-08-07 against `https://novarouteai.com/v1` with the account's own
key, and against OpenRouter as the control. Everything below is observed
behaviour, not vendor documentation. Where a claim could not be verified it is
marked unverified rather than filled in.

## What it is

NovaRoute is an OpenAI-compatible API gateway fronting 31 models, mostly
Chinese-lab families (DeepSeek, Qwen, GLM, Kimi, MiniMax, Doubao). It is a
routing and billing layer, not an inference host: it serves other labs' models
under its own rate card and its own account.

Billing is strictly per token. The pricing page carries input, output and
cache-read columns denominated per 1M tokens, and contains no mention of
per-hour, per-second or per-GPU billing anywhere. This matters because it
settles a question raised during evaluation: NovaRoute is **not** comparable to
Modal or RunPod. Those sell GPU time and leave the serving stack, batching,
autoscaling and cold starts to the buyer. NovaRoute sells completed tokens
behind somebody else's serving stack. The two sit on opposite sides of the
build-versus-buy line and do not compete for the same decision.

## First-pass benchmark

SocTalk's own triage eval, unmodified, pointed at each gateway through a
transparent logging proxy. Identical prompt, output schema, timeout and retry
policy on both sides; the fast and reasoning tiers both set to
`deepseek-v4-flash`, which is the exact model ID returned by `GET /v1/models`.
The proxy records one row per request, so latency and token counts are measured
at the wire rather than reported by the client.

| | NovaRoute | OpenRouter |
|---|---|---|
| requests / HTTP OK / failed | 9 / 9 / 0 | 9 / 9 / 0 |
| retry or fallback events | 0 | 0 |
| schema validation errors | 0 | 0 |
| eval cases passed | 11 / 12 | 12 / 12 |
| total latency p50 | 7.74 s | 13.23 s |
| total latency p90 | 9.50 s | 25.09 s |
| total latency max | 11.77 s | 28.57 s |
| prompt / completion tokens | 11292 / 6157 | 14355 / 6172 |

NovaRoute served the same workload in roughly half the wall-clock time, with a
much tighter tail. A separate concurrency probe agreed: five simultaneous
requests completed in 1.5 s wall with latencies between 1.29 s and 1.45 s, while
OpenRouter took 4.2 s wall with a spread of 0.86 s to 4.19 s.

Two numbers need a caveat rather than a headline. Time-to-first-byte reads as
7.74 s on NovaRoute and 1.20 s on OpenRouter, but that gap is an artifact:
OpenRouter emits a `: OPENROUTER PROCESSING` keepalive comment before any
content, so its first byte is not a first token. These are non-streaming
requests and total latency is the honest comparison. Separately, the single
NovaRoute eval miss is `rare-outbound-ambiguous`, a case whose golden accepts
either `needs_more_info` or `escalate`; the same model on the same gateway
passed it in an earlier run. That is run-to-run variance on a deliberately
ambiguous case, not a gateway defect.

## Protocol compatibility

Probed per model with single minimal calls.

| capability | NovaRoute | OpenRouter |
|---|---|---|
| tool / function calling | yes | yes |
| `response_format: json_object` | yes | yes |
| `response_format: json_schema` (strict) | yes, except `qwen3.7-flash` | yes |
| SSE streaming | yes | yes (after a keepalive comment) |
| cached / reasoning token detail | yes | partial, see below |
| per-call cost reporting | **no** | yes, on request |
| model metadata in `GET /v1/models` | id, display name, type only | pricing, context length, supported parameters |
| model catalogue size | 31 | 400 |

Error shapes are OpenAI-standard on both. NovaRoute returns `{"error":
{"message", "type"}}` with types like `model_not_found` and
`invalid_request_error`, and uses 404 for an unknown model; OpenRouter returns
`{"error": {"message", "code", "metadata"}}` and uses 400. SocTalk's
`classify_llm_error` branches on HTTP status rather than body shape, so both map
correctly without changes.

## Findings that affect SocTalk

**`qwen3.7-flash` cannot do structured output here.** Every `json_object` and
`json_schema` request is rejected with `400 'messages' must contain the word
'json' in some form`. This is an upstream Qwen constraint surfacing through the
gateway. In the eval it produced 9 of 9 failures across routing and verdict; the
3 triage-policy cases passed only because that stage does not call an LLM. The
model is unusable for SocTalk's triage path as things stand, and should not be
offered as a tier option.

**Model availability is account-scoped and can change under a running job.**
`glm-5.2` returned `404 model_not_found` — "not supported by any configured
account in this group" — intermittently, mid-run, having already served
requests successfully in the same session. It also produced a 429 and a 503 in
the same window. SocTalk maps a 404 to `provider_error`, which is terminal, so a
run that meets this fails rather than retries. Any model chosen here should be
smoke-tested repeatedly over time, not once.

**Prompt caching works and is reported accurately.** A repeated 7216-token
prefix came back with `cached_tokens: 7168` on the second call, and the billed
amount dropped correspondingly. OpenRouter's DeepSeek route reported
`cached_tokens: 0` on both calls while charging 37% less on the second, so
caching happened but was invisible in the token detail. The practical
consequence is that SocTalk's cost estimates are accurate on NovaRoute and
would over-state on OpenRouter, which is exactly the case where OpenRouter's
reported actual takes over instead.

**No per-call cost field.** NovaRoute returns no `usage.cost`, so SocTalk must
price its calls from the catalog and there is no per-call figure to reconcile
against. There is, however, an undocumented `GET /v1/usage` endpoint returning
wallet balance plus a `model_stats` array with per-model cumulative requests,
token counts split by cache read and write, and `actual_cost`. That is a viable
daily reconciliation source for drift detection even though it cannot attribute
cost to a run.

## Rate card correction

The catalog rates seeded for NovaRoute models were wrong, uniformly about 61%
too high, because the pricing page was scraped with a row misalignment that
paired each model's name with the next model's prices. They were corrected by
measuring the account ledger directly: balance and `model_stats.actual_cost`
before and after calls with known token splits, solving for the rates.

| model | was | measured | cache read |
|---|---|---|---|
| `deepseek-v4-flash` | $0.206 / $0.412 | $0.1275 / $0.2550 | $0.0255 (20% of input) |
| `kimi-k2.6` | $1.3382 / $5.5588 | $0.8288 / $3.4425 | $0.0829 (10%) |
| `glm-5.2` | $1.6471 / $5.7647 | $1.0200 / $3.5700 | $0.2550 (25%) |

`qwen3.7-plus` was left unverified and is flagged as such in the seed. Its
per-call balance delta read as exactly zero twice, which `model_stats` later
contradicted, so the wallet endpoint settles late and that measurement method is
unreliable for it.

Two things follow. Cache-read rates vary from 10% to 25% of the input rate
across models, so the 10% default SocTalk falls back to when a catalog row
carries no cache dimension is wrong for most of them; the corrected rows now
carry explicit `cache_read_per_mtok`. And a published price page is not a
billing source — measure the ledger.

For reference, OpenRouter lists `deepseek/deepseek-v4-flash` at $0.140 / $0.280
with cache reads at $0.028, so the two gateways are within about 10% of each
other on this model rather than the 1.6x the bad seed implied.

## Recommendation

NovaRoute is a technically sound OpenAI-compatible backend for SocTalk on
`deepseek-v4-flash`: full tool calling, strict structured output, accurate cache
reporting, and roughly half OpenRouter's latency at comparable cost and
accuracy. The two real gaps are the absent per-call cost field, which forces
catalog-based estimation, and account-scoped model availability that can fail a
run terminally.

The thin `GET /v1/models` response also means the catalog cannot be prefilled
from the gateway the way OpenRouter's can, so NovaRoute rows have to be seeded
by measurement and re-checked when prices move.

Do not treat NovaRoute as an alternative to Modal or RunPod. If the question is
whether to rent GPUs and run our own serving stack, that is a different
evaluation with different variables, and nothing here speaks to it.
