<script lang="ts">
	import { onMount } from 'svelte';
	import {
		tenantsApi,
		type TenantLlmRead,
		type TenantLlmUpdate,
		type TenantLlmTierRead,
		type TenantLlmTierWrite,
		type LlmDecodingMode
	} from '$lib/api/tenants';
	import { addToast } from '$lib/stores';
	import { m } from '$lib/paraglide/messages';

	// MSSP-side LLM configuration panel for the tenant detail page. Shows the
	// masked config (GET .../llm — the plaintext key is NEVER returned, only
	// has_api_key + an api_key_preview tail) and offers an in-place edit form
	// that PATCHes only the fields the operator actually changed. A blank
	// "Replace API key" field means "leave unchanged" — the api_key field is
	// OMITTED from the payload entirely. Clearing the key (DELETE .../llm/api-key)
	// requires an inline confirm step.
	//
	// SECURITY: the key value must never reach toasts, console logs, or any
	// non-password input — only the masked preview from the server is rendered.
	export let tenantId: string;

	let read: TenantLlmRead | null = null;
	let readError: string | null = null;
	let loadingRead = true;

	let collapsed = false;
	let editing = false;
	let saving = false;

	// Inline confirm step for the destructive clear-key action.
	let confirmingClear = false;
	let clearing = false;

	interface LlmForm {
		provider: string;
		base_url: string;
		model: string;
		// Per-tier overrides — '' in the form means "no override" (the tier
		// falls back to the primary model).
		fast_model: string;
		reasoning_model: string;
		// Tenant-global default sampling for the router/supervisor tier. Kept as
		// strings so an emptied input can be validated before coercion.
		temperature: string;
		max_tokens: string;
		// Per-tenant case-run budget caps — '' = clear to the worker default.
		api_key: string;
	}

	// Catalog rates for the model being typed (#141 phase 4). The catalog is a
	// CONVENIENCE here, not an authority: it prefills what we know so the
	// operator can confirm or correct it. Nothing found means blank fields they
	// fill in themselves.
	//
	// `priceEdited` is the whole point of the two-field dance: a prefilled rate
	// is only PERSISTED as a tenant override once the operator actually changes
	// it. Saving every accepted suggestion would freeze a copy of today's
	// catalog on the tenant, so a later price correction would stop reaching
	// them — silently, because the numbers looked right when they saved.
	let priceSuggestion: {
		found: boolean;
		exact: boolean;
		input_per_mtok: number | null;
		output_per_mtok: number | null;
		source: string | null;
		note: string | null;
	} | null = null;
	let priceInput = '';
	let priceOutput = '';
	let priceEdited = false;
	// Keyed on the backend too, not just the model: the same model string costs
	// different amounts through different providers, so changing provider or
	// base_url must re-look-up rather than keep the previous backend's rates
	// (Codex review of phases 4-5).
	let priceLookupFor = '';

	function resetPriceState(): void {
		// Called whenever an edit session starts or ends. Without this,
		// priceEdited stayed true across sessions, so a later unrelated edit —
		// or an ACCEPTED suggestion — still shipped model_prices and wrote an
		// override nobody asked for.
		priceSuggestion = null;
		priceInput = '';
		priceOutput = '';
		priceEdited = false;
		priceLookupFor = '';
	}

	async function lookupPrice(): Promise<void> {
		const model = formData.model.trim();
		const key = `${formData.provider}|${formData.base_url.trim()}|${model}`;
		if (!model || !tenantId || key === priceLookupFor) return;
		priceLookupFor = key;
		try {
			const s = await tenantsApi.priceSuggestion(tenantId, {
				model,
				provider: formData.provider,
				base_url: formData.base_url.trim() || undefined
			});
			priceSuggestion = s;
			if (!priceEdited) {
				priceInput = s.input_per_mtok != null ? String(s.input_per_mtok) : '';
				priceOutput = s.output_per_mtok != null ? String(s.output_per_mtok) : '';
			}
		} catch {
			// A lookup failure must not block editing the model; the operator can
			// still type rates, and the save-time gate still has the final say.
			priceSuggestion = null;
		}
	}

	let formData: LlmForm = {
		provider: 'openai-compatible',
		base_url: '',
		model: '',
		fast_model: '',
		reasoning_model: '',
		temperature: '',
		max_tokens: '',
		api_key: ''
	};
	let formError: string | null = null;

	// --- Per-tier "model chain" editor (issue #12 / #4) -----------------
	// A hybrid tenant routes the fast (router) and/or reasoning (verdict)
	// tier to a DEDICATED backend — its own provider/base_url/model/engine/
	// decoding + optional own key — instead of the primary provider. When a
	// tier has a dedicated backend it supersedes the simple fast/thinking
	// model override above (the tier's own model wins at render time).
	const TIER_KEYS = ['fast', 'reasoning'] as const;
	type TierKey = (typeof TIER_KEYS)[number];
	// Message-function REFS, called at render/validation time — never evaluate
	// messages at module scope (#52).
	const TIER_LABELS: Record<TierKey, () => string> = {
		fast: m.ten_llm_tier_fast,
		reasoning: m.ten_llm_tier_reasoning
	};
	const DECODING_MODES: LlmDecodingMode[] = [
		'auto',
		'none',
		'tool_use',
		'json_schema_strict',
		'json_object',
		'guided_json',
		'guided_grammar'
	];

	interface TierForm {
		enabled: boolean;
		provider: 'openai-compatible' | 'anthropic';
		base_url: string;
		model: string;
		engine: '' | 'frontier' | 'openai_compatible' | 'vllm' | 'sglang';
		decoding_mode: '' | LlmDecodingMode;
		// Per-tier sampling override — '' = inherit the caller default. Strings so
		// an emptied input validates before coercion.
		temperature: string;
		max_tokens: string;
		// Whether a key is already stored for this tier (from the sanitized read).
		has_api_key: boolean;
		// New key input — blank = keep the stored key (keep/replace/clear).
		api_key: string;
		// Explicit "reuse the primary credential" — clears the tier's own key.
		clear_key: boolean;
	}

	function blankTier(): TierForm {
		return {
			enabled: false,
			provider: 'openai-compatible',
			base_url: '',
			model: '',
			engine: '',
			decoding_mode: '',
			temperature: '',
			max_tokens: '',
			has_api_key: false,
			api_key: '',
			clear_key: false
		};
	}

	let tierForms: Record<TierKey, TierForm> = {
		fast: blankTier(),
		reasoning: blankTier()
	};

	function seedTier(r: TenantLlmTierRead | undefined): TierForm {
		if (!r) return blankTier();
		return {
			enabled: true,
			provider: (r.provider as TierForm['provider']) ?? 'openai-compatible',
			base_url: r.base_url ?? '',
			model: r.model ?? '',
			engine: (r.engine as TierForm['engine']) ?? '',
			decoding_mode: (r.decoding_mode as LlmDecodingMode) ?? '',
			temperature: r.temperature != null ? String(r.temperature) : '',
			max_tokens: r.max_tokens != null ? String(r.max_tokens) : '',
			has_api_key: r.has_api_key,
			api_key: '',
			clear_key: false
		};
	}

	// A tier's backend differs from the stored read (structural OR key touched).
	function tierDiffers(f: TierForm, r: TenantLlmTierRead | undefined): boolean {
		if (!r) return true; // newly enabled
		return (
			f.provider !== r.provider ||
			f.base_url.trim() !== (r.base_url ?? '') ||
			f.model.trim() !== (r.model ?? '') ||
			(f.engine || null) !== (r.engine ?? null) ||
			(f.decoding_mode || null) !== (r.decoding_mode ?? null) ||
			f.temperature.trim() !== (r.temperature != null ? String(r.temperature) : '') ||
			f.max_tokens.trim() !== (r.max_tokens != null ? String(r.max_tokens) : '') ||
			!!f.api_key.trim() ||
			f.clear_key
		);
	}

	// Build the tiers half of the PATCH: ``undefined`` = omit (unchanged),
	// ``{}`` = clear back to single-provider, a map = replace (backend merges
	// per-tier keys via keep/replace/clear).
	function buildTiers(): Record<string, TenantLlmTierWrite> | undefined {
		const readTiers = read?.tiers ?? null;
		const readEnabled = readTiers ? Object.keys(readTiers) : [];
		const enabled = TIER_KEYS.filter((k) => tierForms[k].enabled);
		let changed =
			enabled.length !== readEnabled.length || enabled.some((k) => !readEnabled.includes(k));
		for (const k of enabled) {
			if (tierDiffers(tierForms[k], readTiers?.[k])) changed = true;
		}
		if (!changed) return undefined;
		if (enabled.length === 0) return readTiers ? {} : undefined;
		const out: Record<string, TenantLlmTierWrite> = {};
		for (const k of enabled) {
			const f = tierForms[k];
			const t: TenantLlmTierWrite = {
				provider: f.provider,
				base_url: f.base_url.trim(),
				model: f.model.trim()
			};
			if (f.engine) t.engine = f.engine;
			if (f.decoding_mode) t.decoding_mode = f.decoding_mode;
			// Per-tier sampling: send only when set (blank = inherit the default).
			if (f.temperature.trim() !== '') t.temperature = Number(f.temperature);
			if (f.max_tokens.trim() !== '') t.max_tokens = Number(f.max_tokens);
			// Key: a typed value replaces; an explicit clear sends ''; otherwise
			// omit so the backend carries the stored key forward. Trim so a
			// whitespace-only entry is treated as "no new key" (keep), matching
			// the backend's whitespace = clear/keep handling rather than sending
			// blanks that silently clear a same-provider tier's own key.
			const newKey = f.api_key.trim();
			if (newKey) t.api_key_plain = newKey;
			else if (f.clear_key) t.api_key_plain = '';
			out[k] = t;
		}
		return out;
	}

	async function loadRead(): Promise<void> {
		loadingRead = true;
		readError = null;
		try {
			read = await tenantsApi.getLlm(tenantId);
		} catch (e) {
			readError = e instanceof Error ? e.message : m.ten_llm_load_failed();
		} finally {
			loadingRead = false;
		}
	}

	function toggleCollapsed(): void {
		collapsed = !collapsed;
	}

	function startEdit(): void {
		resetPriceState();
		// Seed the form from the masked read. The key field stays blank — a blank
		// key means "leave unchanged" so we never round-trip a placeholder.
		formData = {
			provider: read?.provider ?? 'openai-compatible',
			base_url: read?.base_url ?? '',
			model: read?.model ?? '',
			// null (no override) seeds as '' — emptying a previously-set input
			// later diffs as a clear ('' sent), while staying empty is unchanged.
			fast_model: read?.fast_model ?? '',
			reasoning_model: read?.reasoning_model ?? '',
			temperature: read != null ? String(read.temperature) : '',
			max_tokens: read != null ? String(read.max_tokens) : '',
			// null (worker default) seeds blank; a set cap seeds its value.
			api_key: ''
		};
		// Seed the per-tier chain editor from the sanitized read.tiers map.
		tierForms = {
			fast: seedTier(read?.tiers?.fast),
			reasoning: seedTier(read?.tiers?.reasoning)
		};
		formError = null;
		editing = true;
	}

	function cancelEdit(): void {
		resetPriceState();
		editing = false;
		formData = { ...formData, api_key: '' };
		formError = null;
	}

	async function save(): Promise<void> {
		formError = null;
		const baseUrl = formData.base_url.trim();
		if (baseUrl && !/^https?:\/\//.test(baseUrl)) {
			formError = m.ten_llm_err_base_url();
			return;
		}
		// Tenant-global sampling bounds (mirror the backend + chart schema). Both
		// fields are required — they always carry a value (the read seeds the
		// current setting), so a blank is an explicit clear with no meaning; treat
		// it as an error rather than silently no-op'ing the edit.
		const tempStr = formData.temperature.trim();
		if (tempStr === '') {
			formError = m.ten_llm_err_temp_required();
			return;
		}
		const t = Number(tempStr);
		if (!Number.isFinite(t) || t < 0 || t > 2) {
			formError = m.ten_llm_err_temp_range();
			return;
		}
		const maxTokStr = formData.max_tokens.trim();
		if (maxTokStr === '') {
			formError = m.ten_llm_err_max_tokens_required();
			return;
		}
		const maxTok = Number(maxTokStr);
		if (!Number.isInteger(maxTok) || maxTok < 1 || maxTok > 8192) {
			formError = m.ten_llm_err_max_tokens_range();
			return;
		}
		// Per-run TOKEN budget moved to the Agent Run Budget panel (#103); it is
		// no longer edited here (writes to it via this endpoint are rejected).
		// Validate each enabled tier before building the payload — surface the
		// error inline rather than round-tripping to a backend 422 toast.
		for (const k of TIER_KEYS) {
			const f = tierForms[k];
			if (!f.enabled) continue;
			if (!/^https?:\/\//.test(f.base_url.trim())) {
				formError = m.ten_llm_tier_err_base_url({ tier: TIER_LABELS[k]() });
				return;
			}
			if (!f.model.trim()) {
				formError = m.ten_llm_tier_err_model({ tier: TIER_LABELS[k]() });
				return;
			}
			// Per-tier sampling bounds (blank = inherit, so only validate when set).
			if (f.temperature.trim() !== '') {
				const tt = Number(f.temperature);
				if (!Number.isFinite(tt) || tt < 0 || tt > 2) {
					formError = m.ten_llm_tier_err_temp({ tier: TIER_LABELS[k]() });
					return;
				}
			}
			if (f.max_tokens.trim() !== '') {
				const mm = Number(f.max_tokens);
				if (!Number.isInteger(mm) || mm < 1 || mm > 8192) {
					formError = m.ten_llm_tier_err_max_tokens({ tier: TIER_LABELS[k]() });
					return;
				}
			}
			// A tier on a different provider than the primary must carry its own
			// key (the tenant mounts only the primary credential). Catch it here
			// with an actionable message; the backend enforces it too (422).
			const primary = formData.provider;
			const crossProvider = f.provider !== primary;
			const willHaveKey = f.api_key.trim() ? true : f.clear_key ? false : f.has_api_key;
			if (crossProvider && !willHaveKey) {
				formError = m.ten_llm_tier_err_cross_provider({
					tier: TIER_LABELS[k](),
					provider: f.provider,
					primary
				});
				return;
			}
		}
		saving = true;
		try {
			// Changed-fields-only patch: compare against the current read and only
			// send what the operator actually edited. A blank api_key is OMITTED
			// (never sent) so the stored secret is preserved.
			const payload: TenantLlmUpdate = {};
			if (read && formData.provider !== read.provider) {
				payload.provider = formData.provider as TenantLlmUpdate['provider'];
			}
			if (read && baseUrl !== read.base_url) payload.base_url = baseUrl;
			if (read && formData.model !== read.model) payload.model = formData.model;
			// Tri-state per-tier overrides: compare against ``read.x ?? ''`` so an
			// empty input over a null read is "unchanged" (omitted), not a
			// spurious clear. A real clear (input emptied over a set override)
			// sends '' so the backend NULLs the column; a set sends the trimmed
			// value.
			const fastModel = formData.fast_model.trim();
			if (read && fastModel !== (read.fast_model ?? '')) payload.fast_model = fastModel;
			const reasoningModel = formData.reasoning_model.trim();
			if (read && reasoningModel !== (read.reasoning_model ?? '')) {
				payload.reasoning_model = reasoningModel;
			}
			// Tenant-global sampling — send only when changed from the read.
			if (read && tempStr !== '' && Number(tempStr) !== read.temperature) {
				payload.temperature = Number(tempStr);
			}
			if (read && maxTokStr !== '' && Number(maxTokStr) !== read.max_tokens) {
				payload.max_tokens = Number(maxTokStr);
			}
			if (formData.api_key) payload.api_key = formData.api_key;
			// Rates ride along ONLY if the operator changed them. An accepted
			// suggestion is left unsaved on purpose: persisting it would pin a copy
			// of today's catalog to this tenant, and a later correction would then
			// stop reaching them — invisibly, because the numbers were right when
			// they saved (#141 phase 4).
			if (priceEdited && priceInput.trim() && priceOutput.trim()) {
				const inNum = Number(priceInput);
				const outNum = Number(priceOutput);
				if (!Number.isFinite(inNum) || !Number.isFinite(outNum) || inNum < 0 || outNum < 0) {
					formError = 'Rates must be non-negative numbers in $/Mtok.';
					return;
				}
				// Qualified key, not a bare model name. A bare key applies to that
				// model at EVERY backend, and the same model string legitimately
				// costs different amounts through different gateways — which is
				// why the resolver grew qualified keys in the first place (Codex
				// review of phases 4-5).
				//
				// `*` for the vendor slug: the form knows the protocol and the
				// URL, not which upstream a gateway routes to, and the resolver
				// treats `kind:*:model` as covering that protocol.
				const kind =
					formData.provider === 'anthropic' ? 'anthropic' : 'openai_compatible';
				payload.model_prices = {
					...(read?.model_prices ?? {}),
					[`${kind}:*:${formData.model.trim()}`]: { input: inNum, output: outNum }
				};
			}

			// Per-tier chain: undefined = omit (unchanged), {} = clear, map = replace.
			const tiersPayload = buildTiers();
			if (tiersPayload !== undefined) payload.tiers = tiersPayload;

			if (Object.keys(payload).length === 0) {
				// Nothing changed — close the form without a no-op PATCH.
				editing = false;
				resetPriceState();
				return;
			}

			read = await tenantsApi.updateLlm(tenantId, payload);
			editing = false;
			resetPriceState();
			formData = { ...formData, api_key: '' };
			addToast({
				type: 'success',
				title: m.ten_llm_toast_title(),
				message: m.ten_llm_updated()
			});
		} catch (e) {
			addToast({
				type: 'error',
				title: m.ten_llm_toast_title(),
				message: e instanceof Error ? e.message : String(e)
			});
		} finally {
			saving = false;
		}
	}

	async function clearKey(): Promise<void> {
		clearing = true;
		try {
			await tenantsApi.clearLlmKey(tenantId);
			confirmingClear = false;
			addToast({
				type: 'success',
				title: m.ten_llm_toast_title(),
				message: m.ten_llm_key_cleared()
			});
			await loadRead(); // re-fetch the masked state (has_api_key now false)
		} catch (e) {
			addToast({
				type: 'error',
				title: m.ten_llm_toast_title(),
				message: e instanceof Error ? e.message : String(e)
			});
		} finally {
			clearing = false;
		}
	}

	onMount(() => {
		void loadRead();
	});
</script>

<div class="card p-4" data-testid="llm-config-panel">
	<div class="flex items-center justify-between mb-4">
		<button
			class="h4 flex items-center gap-2"
			data-testid="llm-collapse-toggle"
			on:click={toggleCollapsed}
			aria-expanded={!collapsed}
		>
			<span class="opacity-60">{collapsed ? '▸' : '▾'}</span>
			{m.ten_llm_title()}
		</button>
		{#if !collapsed && !editing && !loadingRead}
			<button class="btn btn-sm variant-soft-primary" data-testid="llm-edit" on:click={startEdit}>
				{m.common_edit()}
			</button>
		{/if}
	</div>

	{#if !collapsed}
		{#if loadingRead}
			<div class="flex items-center gap-3 text-sm opacity-70">
				<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current"></span>
				<span>{m.common_loading()}</span>
			</div>
		{:else if readError}
			<div class="text-error-500 text-sm" data-testid="llm-error">{readError}</div>
		{:else if editing}
			<!-- In-place edit form — changed-fields-only PATCH; blank key = keep. -->
			<form class="space-y-4" on:submit|preventDefault={save} data-testid="llm-edit-form">
				<label class="label">
					<span class="text-sm">{m.ten_llm_provider()}</span>
					<select name="provider" class="select" bind:value={formData.provider}>
						<option value="openai-compatible">{m.ten_llm_provider_openai_compatible()}</option>
						<option value="anthropic">{m.ten_llm_provider_anthropic()}</option>
					</select>
				</label>
				<label class="label">
					<span class="text-sm">{m.ten_llm_base_url()}</span>
					<input
						name="base_url"
						class="input"
						bind:value={formData.base_url}
						placeholder="https://api.openai.com/v1"
					/>
				</label>
				{#if formError}
					<div class="text-error-500 text-sm" data-testid="llm-form-error">{formError}</div>
				{/if}
				<label class="label">
					<span class="text-sm">{m.ten_llm_model()}</span>
					<input
						name="model"
						class="input"
						bind:value={formData.model}
						placeholder="gpt-4o"
						data-testid="llm-model"
						on:blur={lookupPrice}
					/>
				</label>
				<!-- Rates for the model above. Prefilled from the catalog where we
				     know them, blank where we do not, and only SAVED as a tenant
				     override if the operator changes them (#141 phase 4). -->
				<div class="rounded border border-surface-500/20 p-3" data-testid="llm-price">
					<div class="text-sm mb-2">
						{#if priceSuggestion?.found && priceSuggestion.exact}
							<span class="opacity-70" data-testid="llm-price-source">
								Rates from the price catalog{priceSuggestion.source
									? ` (${priceSuggestion.source})`
									: ''}.
							</span>
						{:else if priceSuggestion?.found}
							<span class="text-warning-500" data-testid="llm-price-source">
								{priceSuggestion.note ??
									"Vendor list price — your gateway may charge differently."}
							</span>
						{:else if priceSuggestion}
							<span class="text-warning-500" data-testid="llm-price-source">
								No price is known for this model. Enter its rates, or it cannot be
								used while cost accounting is on.
							</span>
						{:else}
							<span class="opacity-70">Rates are looked up when you leave the model field.</span>
						{/if}
					</div>
					<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
						<label class="label">
							<span class="text-sm opacity-70">Input $/Mtok</span>
							<input
								class="input font-mono"
								inputmode="decimal"
								bind:value={priceInput}
								data-testid="llm-price-input"
								on:input={() => (priceEdited = true)}
							/>
						</label>
						<label class="label">
							<span class="text-sm opacity-70">Output $/Mtok</span>
							<input
								class="input font-mono"
								inputmode="decimal"
								bind:value={priceOutput}
								data-testid="llm-price-output"
								on:input={() => (priceEdited = true)}
							/>
						</label>
					</div>
					{#if priceEdited}
						<p class="text-xs opacity-60 mt-2" data-testid="llm-price-override-note">
							Saved as a per-tenant override. Catalog price corrections will no
							longer apply to this model for this tenant.
						</p>
					{/if}
				</div>
				<label class="label">
					<span class="text-sm">{m.ten_llm_fast_model()}</span>
					<input
						name="fast_model"
						class="input"
						bind:value={formData.fast_model}
						placeholder={m.ten_llm_use_primary_model_hint()}
					/>
					<span class="text-xs opacity-60">{m.ten_llm_use_primary_model_hint()}</span>
				</label>
				<label class="label">
					<span class="text-sm">{m.ten_llm_thinking_model()}</span>
					<input
						name="reasoning_model"
						class="input"
						bind:value={formData.reasoning_model}
						placeholder={m.ten_llm_use_primary_model_hint()}
					/>
					<span class="text-xs opacity-60">{m.ten_llm_use_primary_model_hint()}</span>
				</label>
				<div class="grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="llm-sampling">
					<label class="label">
						<span class="text-sm">{m.ten_llm_temperature()}</span>
						<input
							name="temperature"
							class="input"
							type="text"
							inputmode="decimal"
							bind:value={formData.temperature}
							placeholder="0.0"
						/>
						<span class="text-xs opacity-60">{m.ten_llm_temp_hint()}</span>
					</label>
					<label class="label">
						<span class="text-sm">{m.ten_llm_max_tokens()}</span>
						<input
							name="max_tokens"
							class="input"
							type="text"
							inputmode="numeric"
							bind:value={formData.max_tokens}
							placeholder="4096"
						/>
						<span class="text-xs opacity-60">{m.ten_llm_max_tokens_hint()}</span>
					</label>
				</div>
				<label class="label">
					<span class="text-sm">{m.ten_llm_replace_api_key()}</span>
					<input
						name="api_key"
						type="password"
						class="input"
						placeholder={m.ten_leave_blank_to_keep()}
						autocomplete="off"
						bind:value={formData.api_key}
					/>
				</label>

				<!-- Per-tier "model chain" editor: route the fast (router) and/or
				     reasoning (verdict) tier to a dedicated backend. -->
				<div class="pt-3 border-t border-surface-500/20" data-testid="llm-chain-editor">
					<div class="text-sm font-semibold">{m.ten_llm_chain_title()}</div>
					<p class="text-xs opacity-60 mb-3">
						{m.ten_llm_chain_hint()}
					</p>
					{#each TIER_KEYS as tk}
						<div class="mb-3 rounded border border-surface-500/20 p-3" data-testid={`llm-tier-${tk}`}>
							<label class="flex items-center gap-2">
								<input
									type="checkbox"
									class="checkbox"
									data-testid={`llm-tier-${tk}-enabled`}
									bind:checked={tierForms[tk].enabled}
								/>
								<span class="text-sm font-medium">{TIER_LABELS[tk]()}</span>
								<span class="text-xs opacity-60">{m.ten_llm_dedicated_backend()}</span>
							</label>
							{#if tierForms[tk].enabled}
								<div class="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
									<label class="label">
										<span class="text-xs">{m.ten_llm_provider()}</span>
										<select
											class="select select-sm"
											data-testid={`llm-tier-${tk}-provider`}
											bind:value={tierForms[tk].provider}
										>
											<option value="openai-compatible">{m.ten_llm_provider_openai_compatible()}</option>
											<option value="anthropic">{m.ten_llm_provider_anthropic()}</option>
										</select>
									</label>
									<label class="label">
										<span class="text-xs">{m.ten_llm_engine()}</span>
										<select
											class="select select-sm"
											data-testid={`llm-tier-${tk}-engine`}
											bind:value={tierForms[tk].engine}
										>
											<option value="">{m.ten_llm_auto()}</option>
											<option value="frontier">frontier</option>
											<option value="openai_compatible">openai_compatible</option>
											<option value="vllm">vllm</option>
											<option value="sglang">sglang</option>
										</select>
									</label>
									<label class="label md:col-span-2">
										<span class="text-xs">{m.ten_llm_base_url()}</span>
										<input
											class="input"
											data-testid={`llm-tier-${tk}-base-url`}
											bind:value={tierForms[tk].base_url}
											placeholder="http://sglang.internal:8000/v1"
										/>
									</label>
									<label class="label">
										<span class="text-xs">{m.ten_llm_model()}</span>
										<input
											class="input"
											data-testid={`llm-tier-${tk}-model`}
											bind:value={tierForms[tk].model}
											placeholder="qwen3-32b"
										/>
									</label>
									<label class="label">
										<span class="text-xs">{m.ten_llm_decoding_mode()}</span>
										<select
											class="select select-sm"
											data-testid={`llm-tier-${tk}-decoding`}
											bind:value={tierForms[tk].decoding_mode}
										>
											<option value="">{m.ten_llm_auto_resolver()}</option>
											{#each DECODING_MODES as dm}
												<option value={dm}>{dm}</option>
											{/each}
										</select>
									</label>
									<label class="label">
										<span class="text-xs">{m.ten_llm_temperature()}</span>
										<input
											class="input"
											type="text"
											inputmode="decimal"
											data-testid={`llm-tier-${tk}-temperature`}
											bind:value={tierForms[tk].temperature}
											placeholder={m.ten_llm_inherit()}
										/>
									</label>
									<label class="label">
										<span class="text-xs">{m.ten_llm_max_tokens()}</span>
										<input
											class="input"
											type="text"
											inputmode="numeric"
											data-testid={`llm-tier-${tk}-max-tokens`}
											bind:value={tierForms[tk].max_tokens}
											placeholder={m.ten_llm_inherit()}
										/>
									</label>
									<label class="label md:col-span-2">
										<span class="text-xs">
											{tierForms[tk].has_api_key ? m.ten_llm_replace_tier_key() : m.ten_llm_tier_key()}
										</span>
										<input
											type="password"
											class="input"
											data-testid={`llm-tier-${tk}-api-key`}
											autocomplete="off"
											placeholder={tierForms[tk].has_api_key
												? m.ten_leave_blank_to_keep()
												: m.ten_llm_blank_reuse_primary()}
											bind:value={tierForms[tk].api_key}
											disabled={tierForms[tk].clear_key}
										/>
										{#if tierForms[tk].has_api_key}
											<label class="flex items-center gap-2 mt-1">
												<input
													type="checkbox"
													class="checkbox"
													data-testid={`llm-tier-${tk}-clear-key`}
													bind:checked={tierForms[tk].clear_key}
												/>
												<span class="text-xs opacity-70">
													{m.ten_llm_clear_tier_key_label()}
												</span>
											</label>
										{/if}
									</label>
								</div>
							{/if}
						</div>
					{/each}
				</div>
				<div class="flex gap-2">
					<button
						type="submit"
						class="btn btn-sm variant-filled-primary"
						data-testid="llm-save"
						disabled={saving}
					>
						{#if saving}
							<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
						{/if}
						{m.common_save()}
					</button>
					<button
						type="button"
						class="btn btn-sm variant-ghost-surface"
						data-testid="llm-cancel"
						on:click={cancelEdit}
						disabled={saving}
					>
						{m.common_cancel()}
					</button>
				</div>
			</form>
		{:else if read}
			<!-- Read view — masked; only the server-provided key preview is shown. -->
			<dl class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_provider()}</dt>
					<dd data-testid="llm-provider">{read.provider || '—'}</dd>
				</div>
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_base_url()}</dt>
					<dd class="font-mono text-xs text-right break-all" data-testid="llm-base-url">
						{read.base_url || '—'}
					</dd>
				</div>
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_model()}</dt>
					<dd class="font-mono text-xs" data-testid="llm-model">{read.model || '—'}</dd>
				</div>
				<!-- Per-tier overrides — when unset (null) the effective model is the
				     primary one, rendered as "default (<model>)" so the operator
				     always sees what will actually be used. -->
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_fast_model()}</dt>
					<dd class="font-mono text-xs" data-testid="llm-fast-model">
						{read.fast_model ?? m.ten_llm_default_model({ model: read.model })}
					</dd>
				</div>
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_thinking_model()}</dt>
					<dd class="font-mono text-xs" data-testid="llm-reasoning-model">
						{read.reasoning_model ?? m.ten_llm_default_model({ model: read.model })}
					</dd>
				</div>
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_temperature()}</dt>
					<dd class="font-mono text-xs" data-testid="llm-temperature">{read.temperature}</dd>
				</div>
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_max_tokens()}</dt>
					<dd class="font-mono text-xs" data-testid="llm-max-tokens">{read.max_tokens}</dd>
				</div>
				<div class="flex justify-between gap-3">
					<dt class="opacity-60">{m.ten_llm_api_key()}</dt>
					<dd data-testid="llm-api-key-state">
						{#if read.has_api_key}
							<span class="font-mono text-xs" data-testid="llm-api-key-preview">
								{read.api_key_preview}
							</span>
						{:else}
							<span class="opacity-70" data-testid="llm-shared-key-note">
								{m.ten_llm_shared_key_note()}
							</span>
						{/if}
					</dd>
				</div>
			</dl>

			{#if read.tiers}
				<!-- Per-tier "model chain" read view — one row per dedicated backend. -->
				<div class="mt-4 pt-3 border-t border-surface-500/20" data-testid="llm-chain-view">
					<div class="text-sm font-semibold mb-2">{m.ten_llm_chain_view_title()}</div>
					<div class="space-y-2">
						{#each TIER_KEYS as tk}
							{#if read.tiers[tk]}
								<div class="rounded border border-surface-500/20 p-2 text-xs" data-testid={`llm-chain-${tk}`}>
									<div class="font-medium opacity-80">{TIER_LABELS[tk]()}</div>
									<div class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1 mt-1 font-mono">
										<div><span class="opacity-60">{m.ten_llm_chain_provider_label()}</span> {read.tiers[tk].provider ?? '—'}</div>
										<div><span class="opacity-60">{m.ten_llm_chain_engine_label()}</span> {read.tiers[tk].engine ?? m.ten_llm_auto()}</div>
										<div class="md:col-span-2 break-all"><span class="opacity-60">{m.ten_llm_chain_base_label()}</span> {read.tiers[tk].base_url ?? '—'}</div>
										<div><span class="opacity-60">{m.ten_llm_chain_model_label()}</span> {read.tiers[tk].model ?? '—'}</div>
										<div><span class="opacity-60">{m.ten_llm_chain_decoding_label()}</span> {read.tiers[tk].decoding_mode ?? m.ten_llm_auto()}</div>
										<div><span class="opacity-60">{m.ten_llm_chain_temp_label()}</span> {read.tiers[tk].temperature ?? m.ten_llm_inherit()}</div>
										<div><span class="opacity-60">{m.ten_llm_chain_max_tok_label()}</span> {read.tiers[tk].max_tokens ?? m.ten_llm_inherit()}</div>
										<div class="md:col-span-2">
											<span class="opacity-60">{m.ten_llm_chain_key_label()}</span>
											{read.tiers[tk].has_api_key ? m.ten_llm_own_key() : m.ten_llm_reuses_primary()}
										</div>
									</div>
								</div>
							{/if}
						{/each}
					</div>
				</div>
			{/if}

			{#if read.has_api_key}
				<div class="mt-3">
					{#if confirmingClear}
						<div class="flex items-center gap-2" data-testid="llm-clear-key-confirm-row">
							<span class="text-sm">
								{m.ten_llm_clear_key_confirm_prompt()}
							</span>
							<button
								class="btn btn-sm variant-filled-error"
								data-testid="llm-clear-key-confirm"
								on:click={clearKey}
								disabled={clearing}
							>
								{#if clearing}
									<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
								{/if}
								{m.ten_llm_confirm_clear()}
							</button>
							<button
								class="btn btn-sm variant-ghost-surface"
								data-testid="llm-clear-key-cancel"
								on:click={() => (confirmingClear = false)}
								disabled={clearing}
							>
								{m.common_cancel()}
							</button>
						</div>
					{:else}
						<button
							class="btn btn-sm variant-soft-error"
							data-testid="llm-clear-key"
							on:click={() => (confirmingClear = true)}
						>
							{m.ten_llm_clear_key()}
						</button>
					{/if}
				</div>
			{/if}

			<!-- Rollout semantics so operators know what to expect after Save. -->
			<p class="mt-4 pt-3 border-t border-surface-500/20 text-xs opacity-70" data-testid="llm-rollout-note">
				{m.ten_llm_rollout_note()}
			</p>
		{/if}
	{/if}
</div>
