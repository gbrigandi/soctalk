<!--
  Per-tenant model price overlay (#121) over the install catalog (#125).

  Two halves, and the split is the point:

  * EFFECTIVE — what a new run would actually be stamped with, per role, with
    the source it came from. Read-only, because it is a resolution result, not
    a setting. `source` is the honest part: `catalog` means the install rate
    card, `tenant_override` means a row below, and `unknown` means nothing knew
    this model and the run will be billed by the unknown-model policy.
  * OVERRIDE — the tenant's own rates, editable. Only for BYO-LLM tenants whose
    negotiated price differs from the catalog; most tenants should have none.

  The install catalog itself is deliberately NOT editable here. It is seeded by
  an operator through `soctalk-prices import`, which keeps one auditable path
  for install-wide rates and stops a per-tenant screen from silently repricing
  every other tenant.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { tenantsApi, type TenantLlmRead } from '$lib/api/tenants';

	export let tenantId: string;
	/** MSSP analysts read; only admins write (the backend PATCH would 403). */
	export let canEdit = true;

	type Row = { model: string; input: string; output: string };

	let read: TenantLlmRead | null = null;
	let rows: Row[] = [];
	let loading = true;
	let saving = false;
	let error: string | null = null;
	let saved = false;

	function money(v: number | undefined | null): string {
		if (v == null) return '—';
		if (v === 0) return '$0.00';
		return Math.abs(v) < 0.01 ? `$${v.toFixed(6)}` : `$${v.toFixed(4)}`;
	}

	function rowsFrom(r: TenantLlmRead): Row[] {
		const p = r.model_prices ?? {};
		return Object.entries(p).map(([model, v]) => ({
			model,
			input: String(v.input),
			output: String(v.output)
		}));
	}

	async function load() {
		loading = true;
		error = null;
		try {
			read = await tenantsApi.getLlm(tenantId);
			rows = rowsFrom(read);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	function addRow() {
		rows = [...rows, { model: '', input: '', output: '' }];
		saved = false;
	}

	function removeRow(i: number) {
		rows = rows.filter((_, idx) => idx !== i);
		saved = false;
	}

	/** Build the wire map, or throw a message the operator can act on. */
	function buildPrices(): Record<string, { input: number; output: number }> {
		const out: Record<string, { input: number; output: number }> = {};
		for (const [i, r] of rows.entries()) {
			const model = r.model.trim();
			if (!model) throw new Error(`Row ${i + 1}: model is required.`);
			if (out[model]) throw new Error(`Row ${i + 1}: "${model}" is listed twice.`);
			for (const [label, raw] of [
				['input', r.input],
				['output', r.output]
			] as const) {
				const n = Number(raw.trim());
				if (raw.trim() === '' || !Number.isFinite(n) || n < 0) {
					throw new Error(`Row ${i + 1}: ${label} must be a number of 0 or more.`);
				}
			}
			out[model] = { input: Number(r.input), output: Number(r.output) };
		}
		return out;
	}

	async function save() {
		if (!canEdit) return;
		let prices: Record<string, { input: number; output: number }>;
		try {
			prices = buildPrices();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			return;
		}
		saving = true;
		error = null;
		saved = false;
		try {
			// Replace semantics: an empty map clears the overlay back to catalog
			// rates, which is what removing every row should mean.
			await tenantsApi.updateLlm(tenantId, { model_prices: prices });
			await load();
			saved = true;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			saving = false;
		}
	}

	onMount(load);
</script>

<section class="card p-4" data-testid="model-prices-panel">
	<header class="flex items-center justify-between mb-3">
		<h3 class="h4">Model Prices</h3>
		{#if read?.effective_prices}
			<span class="badge variant-soft" data-testid="model-prices-currency">
				{read.effective_prices.currency} per 1M tokens
			</span>
		{/if}
	</header>

	{#if loading}
		<div class="flex justify-center p-6">
			<div class="animate-spin w-5 h-5 border-2 border-primary-500 border-t-transparent rounded-full"></div>
		</div>
	{:else if error && !read}
		<aside class="alert variant-soft-error"><p>{error}</p></aside>
	{:else if read}
		<h4 class="text-sm font-semibold opacity-70 mb-2">Effective rates for a new run</h4>
		{#if read.effective_prices}
			<div class="overflow-x-auto mb-4">
				<table class="table table-compact w-full" data-testid="effective-prices-table">
					<thead>
						<tr>
							<th>Tier</th><th>Model</th><th>Input</th><th>Output</th>
							<th>Cache read</th><th>Source</th>
						</tr>
					</thead>
					<tbody>
						{#each Object.entries(read.effective_prices.models) as [role, r]}
							<tr data-testid="effective-price-{role}">
								<td class="capitalize">{role}</td>
								<td class="font-mono text-xs">{r.model}</td>
								<td class="font-mono tabular-nums">{money(r.input_per_mtok)}</td>
								<td class="font-mono tabular-nums">{money(r.output_per_mtok)}</td>
								<td class="font-mono tabular-nums">{money(r.cache_read_per_mtok)}</td>
								<td>
									<span
										class="badge {r.source === 'unknown'
											? 'variant-filled-warning'
											: 'variant-soft'}">{r.source}</span
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
			{#if Object.values(read.effective_prices.models).some((r) => r.source === 'unknown')}
				<aside class="alert variant-soft-warning mb-4" data-testid="unknown-price-warning">
					<p class="text-sm">
						A tier has no known rate. Runs on it are billed by the install's
						unknown-model policy, which is deliberately expensive so the gap is
						visible rather than silent. Add an override below, or seed the catalog.
					</p>
				</aside>
			{/if}
		{:else}
			<p class="text-sm opacity-60 mb-4">
				No rates resolved yet — the tenant has no LLM configuration.
			</p>
		{/if}

		<h4 class="text-sm font-semibold opacity-70 mb-2">Tenant overrides</h4>
		<p class="text-xs opacity-50 mb-2">
			Only needed when this tenant's negotiated rate differs from the install
			catalog. Leave empty to bill at catalog rates.
		</p>

		{#if rows.length === 0}
			<p class="text-sm opacity-60 mb-3" data-testid="model-prices-empty">
				No overrides — billing at catalog rates.
			</p>
		{:else}
			<div class="overflow-x-auto mb-3">
				<table class="table table-compact w-full">
					<thead>
						<tr><th>Model</th><th>Input $/1M</th><th>Output $/1M</th><th></th></tr>
					</thead>
					<tbody>
						{#each rows as row, i}
							<tr>
								<td>
									<input
										class="input input-sm font-mono"
										placeholder="deepseek-v4-flash"
										bind:value={row.model}
										disabled={!canEdit || saving}
										data-testid="price-model-{i}"
									/>
								</td>
								<td>
									<input
										class="input input-sm font-mono"
										inputmode="decimal"
										bind:value={row.input}
										disabled={!canEdit || saving}
										data-testid="price-input-{i}"
									/>
								</td>
								<td>
									<input
										class="input input-sm font-mono"
										inputmode="decimal"
										bind:value={row.output}
										disabled={!canEdit || saving}
										data-testid="price-output-{i}"
									/>
								</td>
								<td>
									{#if canEdit}
										<button
											class="btn btn-sm variant-soft"
											on:click={() => removeRow(i)}
											disabled={saving}
											data-testid="price-remove-{i}">Remove</button
										>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if canEdit}
			<div class="flex gap-2">
				<button
					class="btn btn-sm variant-soft"
					on:click={addRow}
					disabled={saving}
					data-testid="price-add">Add model</button
				>
				<button
					class="btn btn-sm variant-filled-primary"
					on:click={save}
					disabled={saving}
					data-testid="price-save">{saving ? 'Saving…' : 'Save prices'}</button
				>
			</div>
			{#if error}
				<p class="text-error-500 text-sm mt-2" data-testid="price-error">{error}</p>
			{/if}
			{#if saved}
				<p class="text-success-500 text-sm mt-2" data-testid="price-saved">
					Saved. Applies to the next run; in-flight runs keep the rates they were
					stamped with.
				</p>
			{/if}
		{/if}
	{/if}
</section>
