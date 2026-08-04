<!--
  Agent Run token budget (#103).

  MSSP mode (tenantId set): editable per-tenant override with Save/Clear, plus
  read-only install default / cap / effective and 24h token spend.
  Tenant mode (no tenantId): read-only effective view; a tenant cannot raise it.

  The override is resolved at run creation and capped at the install max, so a
  change takes effect on the next run with no worker rollout.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type RunBudget } from '$lib/api/client';
	import { formatNumber } from '$lib/i18n/format';

	/** Present = MSSP view for this tenant; absent = tenant read-only view. */
	export let tenantId: string | null = null;
	/** Whether the caller may WRITE the override. MSSP analysts get read-only
	 *  even with a tenantId (the backend PATCH would 403 anyway). */
	export let canEdit = true;

	let budget: RunBudget | null = null;
	let loading = true;
	let error: string | null = null;
	let saving = false;
	let overrideInput = '';

	$: editable = tenantId != null && canEdit;

	async function load() {
		loading = true;
		error = null;
		try {
			// Endpoint by SCOPE (tenantId = MSSP view), not by write permission:
			// an MSSP analyst still reads the MSSP run-budget for this tenant.
			budget =
				tenantId != null
					? await api.runBudget.get(tenantId)
					: await api.tenantRunBudget.get();
			overrideInput = budget.tenant_override != null ? String(budget.tenant_override) : '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function save() {
		if (!editable || !budget) return;
		const raw = overrideInput.trim();
		let override: number | null;
		if (raw === '') {
			override = null; // clear the override
		} else {
			const n = Number(raw);
			if (!Number.isInteger(n) || n < 1000) {
				error = 'Budget must be a whole number of at least 1,000 tokens.';
				return;
			}
			if (n > budget.install_max) {
				error = `Budget must not exceed the install cap of ${formatNumber(budget.install_max)} tokens.`;
				return;
			}
			override = n;
		}
		saving = true;
		error = null;
		try {
			budget = await api.runBudget.update(tenantId as string, override);
			overrideInput = budget.tenant_override != null ? String(budget.tenant_override) : '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			saving = false;
		}
	}

	function clearOverride() {
		overrideInput = '';
		void save();
	}

	onMount(load);
</script>

<section class="card p-4" data-testid="run-budget-panel">
	<header class="flex items-center justify-between mb-3">
		<h3 class="h4">Agent Run Budget</h3>
		{#if budget && !loading}
			<span class="badge variant-soft" data-testid="run-budget-effective">
				Effective: {formatNumber(budget.effective)} tokens/run
			</span>
		{/if}
	</header>

	{#if loading}
		<div class="flex justify-center p-6">
			<div class="animate-spin w-5 h-5 border-2 border-primary-500 border-t-transparent rounded-full"></div>
		</div>
	{:else if error && !budget}
		<aside class="alert variant-soft-error"><p>{error}</p></aside>
	{:else if budget}
		<dl class="grid grid-cols-2 gap-3 text-sm mb-4">
			<div>
				<dt class="opacity-60">Install default</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-default">
					{formatNumber(budget.install_default)}
				</dd>
			</div>
			<div>
				<dt class="opacity-60">Install cap</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-cap">
					{formatNumber(budget.install_max)}
				</dd>
			</div>
			<div>
				<dt class="opacity-60">Tenant override</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-override">
					{budget.tenant_override != null ? formatNumber(budget.tenant_override) : '—'}
				</dd>
			</div>
			<div>
				<dt class="opacity-60">Tokens used (24h)</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-spend">
					{formatNumber(budget.spend_24h_tokens)}
				</dd>
			</div>
		</dl>

		{#if editable}
			<div class="flex items-end gap-2">
				<label class="label flex-1">
					<span class="text-sm opacity-70">Override (tokens/run, blank = install default)</span>
					<input
						class="input font-mono"
						type="text"
						inputmode="numeric"
						placeholder={String(budget.install_default)}
						bind:value={overrideInput}
						data-testid="run-budget-input"
						disabled={saving}
					/>
				</label>
				<button
					class="btn variant-filled-primary"
					on:click={save}
					disabled={saving}
					data-testid="run-budget-save"
				>
					{saving ? 'Saving…' : 'Save'}
				</button>
				<button
					class="btn variant-soft"
					on:click={clearOverride}
					disabled={saving || budget.tenant_override == null}
					data-testid="run-budget-clear"
				>
					Clear
				</button>
			</div>
			{#if error}
				<p class="text-error-500 text-sm mt-2" data-testid="run-budget-error">{error}</p>
			{/if}
			<p class="text-xs opacity-50 mt-2">
				Applies to the next run. In-flight runs keep their budget. Cannot exceed the install cap.
			</p>
		{:else}
			<p class="text-xs opacity-50">
				The effective budget is set by your provider. New runs use it; in-flight runs keep theirs.
			</p>
		{/if}
	{/if}
</section>
