/**
 * Tenant lifecycle API. Backed by V1's /api/mssp/tenants surface; only
 * mssp_admin/mssp_analyst roles can read or mutate. UI gating happens
 * via the ``isMsspScope`` store — anything that imports this module
 * must check role first.
 */

const API_BASE = '/api';

class TenantApiError extends Error {
	constructor(public status: number, message: string) {
		super(message);
	}
}

async function _request<T>(endpoint: string, init: RequestInit = {}): Promise<T> {
	const r = await fetch(`${API_BASE}${endpoint}`, {
		credentials: 'include',
		...init,
		headers: {
			'Content-Type': 'application/json',
			...(init.headers ?? {})
		}
	});
	if (!r.ok) {
		const body = await r.text();
		throw new TenantApiError(r.status, body || r.statusText);
	}
	if (r.status === 204) return undefined as unknown as T;
	return (await r.json()) as T;
}

export type TenantProfile = 'poc' | 'persistent' | 'provided' | 'legacy';

export type TenantState =
	| 'pending'
	| 'provisioning'
	| 'active'
	| 'suspended'
	| 'degraded'
	| 'decommissioning'
	| 'archived'
	| 'purged';

export interface Tenant {
	id: string;
	slug: string;
	display_name: string;
	state: TenantState;
	profile?: TenantProfile | null;
	created_at: string;
	state_changed_at: string;
	runtime?: Record<string, unknown> | null;
}

// External SIEM (Wazuh) connection material for the ``provided`` profile —
// the tenant brings their own Wazuh deployment instead of having SocTalk
// provision one. The Wazuh **Indexer** (OpenSearch, :9200) and the **API**
// (manager, :55000) authenticate with separate credentials. ``api_token`` is
// an optional pre-minted manager token; ``verify_ssl`` defaults to true.
// Mirrors the backend ``ExternalSiemOnboard`` model 1:1.
export interface ExternalSiemOnboard {
	indexer_url: string;
	indexer_username: string;
	indexer_password: string;
	api_url: string;
	api_username: string;
	api_password: string;
	api_token?: string;
	verify_ssl: boolean;
}

export interface TenantOnboard {
	slug: string;
	display_name: string;
	profile: 'poc' | 'persistent' | 'provided';
	branding_app_name?: string | null;
	branding_logo_url?: string | null;
	branding_primary_color?: string | null;
	branding_secondary_color?: string | null;
	contact_email?: string | null;
	llm_base_url?: string;
	llm_model?: string;
	// Optional per-tenant LLM credentials. ``llm_provider`` is one of
	// 'openai' | 'anthropic' | 'openai-compatible' (normalized server-side);
	// ``llm_api_key`` is REQUIRED by the backend for profile='provided'
	// (422 otherwise) and optional for poc/persistent (blank → MSSP shared
	// install key). Both are omitted entirely when blank.
	llm_provider?: string;
	llm_api_key?: string;
	// Optional per-tenant model overrides for the fast (cheap/summarize) and
	// reasoning ("Thinking model" in UI copy) tiers. Omitted entirely when
	// blank, mirroring the llm_provider/llm_api_key convention above.
	llm_fast_model?: string;
	llm_reasoning_model?: string;
	// Nested external-SIEM block — only sent for the ``provided`` profile.
	// Supersedes the earlier flat ``wazuh_*`` fields. Omitted entirely for
	// poc/persistent so the controller fills wazuh_url/indexer_url in-cluster.
	external_siem?: ExternalSiemOnboard;
}

export interface LifecycleEvent {
	id: string;
	timestamp: string;
	event_type: string;
	from_state: string | null;
	to_state: string | null;
	actor_id: string | null;
	details: Record<string, unknown>;
}

// Masked view of a tenant's external-SIEM connection. Plaintext secrets are
// NEVER returned — only ``has_*`` booleans signal presence (mirrors the
// backend ``ExternalSiemRead``).
export interface ExternalSiemRead {
	indexer_url: string | null;
	indexer_username: string | null;
	api_url: string | null;
	api_username: string | null;
	has_indexer_password: boolean;
	has_api_password: boolean;
	has_api_token: boolean;
	verify_ssl: boolean;
}

// All-optional credential patch — only the fields the operator actually
// changed are sent. ``null`` / omitted means "leave unchanged"; a blank
// secret is never sent so the existing value is preserved. Mirrors the
// backend ``ExternalSiemPatch``.
export interface ExternalSiemUpdate {
	indexer_url?: string | null;
	indexer_username?: string | null;
	indexer_password?: string | null;
	api_url?: string | null;
	api_username?: string | null;
	api_password?: string | null;
	api_token?: string | null;
	verify_ssl?: boolean | null;
}

// Masked view of a tenant's LLM configuration. The plaintext API key is
// NEVER returned — only ``has_api_key`` signals presence and
// ``api_key_preview`` shows a ``sk-…ABCD`` style tail (empty string when
// no key is set) so the operator can sanity-check WHICH key is in use.
// ``provider`` echoes the stored canonical value. Mirrors the backend
// ``LlmConfigRead`` 1:1.
// Structured-decoding mechanism for a per-tier backend (issue #32). Omitted
// lets the runtime resolver pick per provider. Mirrors the backend
// ``LLMTierConfig.decoding_mode`` Literal.
export type LlmDecodingMode =
	| 'auto'
	| 'none'
	| 'tool_use'
	| 'json_schema_strict'
	| 'json_object'
	| 'guided_json'
	| 'guided_grammar';

export type LlmEngine = 'frontier' | 'openai_compatible' | 'vllm' | 'sglang';

const LLM_DOT_FOLD_RE = /[\u3002\uff0e\uff61]/g;
const SELF_HOSTED_LLM_ENGINES = new Set(['vllm', 'sglang']);

function llmHostMatchesDomain(host: string, domain: string): boolean {
	return host === domain || host.endsWith(`.${domain}`);
}

export function authorityHostForLlmBaseUrl(baseUrl: string | null | undefined): string {
	const raw = (baseUrl ?? '').trim();
	const scheme = raw.match(/^[A-Za-z][A-Za-z0-9+.-]*:\/\//);
	if (!scheme) return '';
	let authority = raw.slice(scheme[0].length).split(/[/?#]/, 1)[0] ?? '';
	const userinfo = authority.lastIndexOf('@');
	if (userinfo >= 0) authority = authority.slice(userinfo + 1);
	if (!authority) return '';
	if (authority.startsWith('[')) {
		const close = authority.indexOf(']');
		if (close < 0) return '';
		return authority.slice(1, close).replace(LLM_DOT_FOLD_RE, '.').toLowerCase();
	}
	let host = authority.split(':', 1)[0]?.replace(LLM_DOT_FOLD_RE, '.').toLowerCase() ?? '';
	if (!host || /\s/.test(host)) return '';
	if (host.endsWith('.')) host = host.slice(0, -1);
	return host;
}

export function providerKindForPriceOverride(
	provider: string | null | undefined,
	baseUrl: string | null | undefined,
	engine: '' | LlmEngine | null | undefined
): string {
	const p = (provider ?? '').trim().toLowerCase();
	if (p === 'anthropic') return 'anthropic';

	const host = authorityHostForLlmBaseUrl(baseUrl);
	if (llmHostMatchesDomain(host, 'openrouter.ai')) return 'openrouter';
	if (llmHostMatchesDomain(host, 'api.openai.com')) return 'openai';
	if (llmHostMatchesDomain(host, 'api.anthropic.com')) return 'anthropic';
	if (SELF_HOSTED_LLM_ENGINES.has((engine ?? '').trim().toLowerCase())) {
		return 'self_hosted';
	}
	if (p === 'openai' || p === 'openai-compatible') return 'openai_compatible';
	return 'openai_compatible';
}

export function priceOverrideKey(
	provider: string | null | undefined,
	baseUrl: string | null | undefined,
	engine: '' | LlmEngine | null | undefined,
	model: string
): string {
	return `${providerKindForPriceOverride(provider, baseUrl, engine)}:*:${model}`;
}

// Sanitized read view of one per-tier LLM backend (the "chain" of a hybrid
// tenant). The plaintext key is NEVER returned — ``has_api_key`` signals its
// presence, matching the top-level ``TenantLlmRead`` convention. Mirrors the
// backend ``_sanitize_tiers`` output.
export interface TenantLlmTierRead {
	provider: string | null;
	base_url: string | null;
	model: string | null;
	engine: LlmEngine | null;
	decoding_mode: LlmDecodingMode | null;
	// Per-tier sampling override — null means the tier inherits its caller
	// default (router → tenant-global sampling; reasoning → tuned constants).
	temperature: number | null;
	max_tokens: number | null;
	has_api_key: boolean;
}

// Write shape for one per-tier backend. Sent on PATCH; the plaintext key
// follows keep/replace/clear semantics: OMIT ``api_key_plain`` to keep the
// stored key, send a non-empty value to replace it, send '' to clear it.
// Mirrors the backend ``LLMTierConfig`` input.
export interface TenantLlmTierWrite {
	provider: 'openai-compatible' | 'anthropic';
	base_url: string;
	model: string;
	engine?: LlmEngine;
	decoding_mode?: LlmDecodingMode;
	// Per-tier sampling override (0–2 / 1–8192). Omit to inherit the default.
	temperature?: number;
	max_tokens?: number;
	api_key_plain?: string;
}

export interface EffectivePriceRole {
	model: string;
	provider_kind: string;
	provider_id: string | null;
	input_per_mtok?: number;
	output_per_mtok?: number;
	cache_read_per_mtok?: number;
	cache_write_per_mtok?: number;
	/** tenant_override | catalog | unknown — where the rate came from. */
	source: string;
	as_of?: string | null;
}

export interface EffectivePrices {
	version: number;
	currency: string;
	resolved_at: string;
	models: Record<string, EffectivePriceRole>;
}

export interface TenantLlmRead {
	provider: string;
	base_url: string;
	model: string;
	engine: LlmEngine | null;
	// Per-tier model overrides — ``null`` means no override is set and the
	// tier falls back to ``model``.
	fast_model: string | null;
	reasoning_model: string | null;
	// Tenant-global default sampling for the router/supervisor tier.
	temperature: number;
	max_tokens: number;
	// Per-tenant case-run budget caps. dollar_budget_per_run is vestigial:
	// the API rejects writes to it and the ceiling lives on the run-budget
	// resource (#128). Kept on the read only so older servers still parse.
	dollar_budget_per_run: number | null;
	token_budget_per_run: number | null;
	// Per-tenant price overlay (#121), USD per MILLION tokens, keyed by model.
	// null when the tenant sets no override and catalog rates apply.
	model_prices: Record<string, { input: number; output: number }> | null;
	// The RESOLVED rates a new run would be stamped with: the snapshot shape
	// {version, currency, resolved_at, models: {fast, reasoning}} where each
	// role carries its rates, provider and source. Read-only.
	effective_prices: EffectivePrices | null;
	has_api_key: boolean;
	api_key_preview: string;
	// Per-tier backends for a hybrid tenant (the model "chain"). ``null`` for a
	// single-provider tenant. Keys are the tier names (``fast`` / ``reasoning``).
	tiers: Record<string, TenantLlmTierRead> | null;
}

// All-optional config patch — only the fields the operator actually
// changed are sent. Omitted means "leave unchanged"; a blank ``api_key``
// is never sent so the existing secret is preserved. Mirrors the
// backend ``LlmConfigUpdate``.
export interface TenantLlmUpdate {
	provider?: 'openai' | 'anthropic' | 'openai-compatible';
	base_url?: string;
	model?: string;
	// Tri-state primary engine: omitted = leave unchanged, '' = clear to a
	// generic gateway/frontier default, non-empty = set the stored engine.
	engine?: '' | LlmEngine;
	api_key?: string;
	// Tri-state per-tier overrides: omitted = leave unchanged, '' = clear
	// the override (revert the tier to the primary ``model``), non-empty =
	// set the override.
	fast_model?: string;
	reasoning_model?: string;
	// Tenant-global default sampling: omitted = unchanged. temperature 0–2,
	// max_tokens 1–8192 (router output cap; bounds enforced server-side).
	temperature?: number;
	max_tokens?: number;
	// Per-tenant case-run budget caps. token_budget_per_run and
	// dollar_budget_per_run are both rejected by the API now (#103, #128);
	// use the run-budget resource.
	token_budget_per_run?: number | null;
	// Price overlay: omitted = unchanged, {} = clear back to catalog rates,
	// a map = replace wholesale. USD per million tokens.
	model_prices?: Record<string, { input: number; output: number }>;
	// Per-tier backends (the model "chain"): omitted = leave unchanged, {} =
	// clear back to single-provider, a map = replace. Per-tier key semantics
	// are keep/replace/clear (see ``TenantLlmTierWrite``).
	tiers?: Record<string, TenantLlmTierWrite>;
}

// Live adapter ingest status — the control plane server-side proxies the
// per-tenant adapter's /health/ready (the browser cannot reach it). On a
// reachable adapter the ingest fields are present; on failure the proxy
// returns ``{ reachable: false, error }`` with HTTP 200.
export interface AdapterStatus {
	reachable?: boolean;
	ok?: boolean;
	alerts_forwarded?: number;
	last_alert_ts?: string | null;
	last_ingest_error?: string | null;
	last_heartbeat_ok?: string | null;
	last_heartbeat_error?: string | null;
	error?: string;
	[key: string]: unknown;
}

export const tenantsApi = {
	list: () => _request<Tenant[]>('/mssp/tenants'),
	get: (id: string) => _request<Tenant>(`/mssp/tenants/${id}`),
	onboard: (body: TenantOnboard) =>
		_request<Tenant>('/mssp/tenants/onboard', {
			method: 'POST',
			body: JSON.stringify(body)
		}),
	retry: (id: string) =>
		_request<unknown>(`/mssp/tenants/${id}:retry`, { method: 'POST' }),
	suspend: (id: string) =>
		_request<Tenant>(`/mssp/tenants/${id}:suspend`, { method: 'POST' }),
	resume: (id: string) =>
		_request<Tenant>(`/mssp/tenants/${id}:resume`, { method: 'POST' }),
	decommission: (id: string) =>
		_request<Tenant>(`/mssp/tenants/${id}:decommission`, { method: 'POST' }),
	events: (id: string, limit = 100) =>
		_request<LifecycleEvent[]>(`/mssp/tenants/${id}/events?limit=${limit}`),
	// External SIEM (Wazuh) connection — masked read, credential patch, and a
	// server-side proxied live adapter status. Available for any profile.
	getExternalSiem: (id: string) =>
		_request<ExternalSiemRead>(`/mssp/tenants/${id}/external-siem`),
	updateExternalSiem: (id: string, payload: ExternalSiemUpdate) =>
		_request<ExternalSiemRead>(`/mssp/tenants/${id}/external-siem`, {
			method: 'PATCH',
			body: JSON.stringify(payload)
		}),
	getAdapterStatus: (id: string) =>
		_request<AdapterStatus>(`/mssp/tenants/${id}/adapter-status`),
	// Per-tenant LLM configuration — masked read, changed-fields-only
	// patch, and an explicit key clear (204, no body).
	getLlm: (id: string) => _request<TenantLlmRead>(`/mssp/tenants/${id}/llm`),
	/** Catalog rates for a model, to PREFILL the rate fields (#141 phase 4).
	 *  `found: false` means we have nothing to offer and the operator types
	 *  them. `exact: false` means these are the VENDOR's rates, not this
	 *  gateway's — clients must read `exact`, not just `found`. */
	priceSuggestion: (
		id: string,
		q: { model: string; provider?: string; base_url?: string; engine?: string }
	) => {
		const p = new URLSearchParams({ model: q.model });
		if (q.provider) p.set('provider', q.provider);
		if (q.base_url) p.set('base_url', q.base_url);
		if (q.engine) p.set('engine', q.engine);
		return _request<{
			model: string;
			found: boolean;
			exact: boolean;
			input_per_mtok: number | null;
			output_per_mtok: number | null;
			source: string | null;
			as_of: string | null;
			note: string | null;
		}>(`/mssp/tenants/${id}/llm/price-suggestion?${p.toString()}`);
	},
	updateLlm: (id: string, payload: TenantLlmUpdate) =>
		_request<TenantLlmRead>(`/mssp/tenants/${id}/llm`, {
			method: 'PATCH',
			body: JSON.stringify(payload)
		}),
	clearLlmKey: (id: string) =>
		_request<void>(`/mssp/tenants/${id}/llm/api-key`, { method: 'DELETE' })
};

export function tenantStateBadge(state: TenantState): string {
	switch (state) {
		case 'active':
			return 'variant-filled-success';
		case 'pending':
		case 'provisioning':
			return 'variant-filled-warning';
		case 'degraded':
		case 'suspended':
			return 'variant-filled-error';
		case 'decommissioning':
		case 'archived':
		case 'purged':
			return 'variant-filled-surface';
		default:
			return 'variant-filled-tertiary';
	}
}
