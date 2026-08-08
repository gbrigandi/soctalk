<!--
  Agent Run budget (#103 tokens, #128 dollars, #129 rolling 24h ceilings).

  MSSP mode (tenantId set): editable per-tenant overrides with Save/Clear, plus
  read-only install default / cap / effective for both dimensions.
  Tenant mode (no tenantId): read-only effective view; a tenant cannot raise it.

  Overrides are resolved at run creation and capped at the install max, so a
  change takes effect on the next run with no worker rollout.

  The daily block matters more than it looks: when that ceiling trips, the
  worker simply stops claiming runs, which is indistinguishable from an idle
  queue unless the spend, the cap, the reason and the reset time are on screen.
  Daily means a CALENDAR day in the tenant's zone, so it clears in one step at
  midnight rather than trickling back as a rolling window would.
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
	let dollarInput = '';
	let dailyTokenInput = '';
	let dailyDollarInput = '';

	$: editable = tenantId != null && canEdit;

	/** Sub-cent ceilings are real (a short run costs fractions of a cent), so
	 *  two decimals would render them as $0.00. Precision follows magnitude.
	 *
	 *  Tolerates a missing value on purpose: an API server predating the dollar
	 *  and 24h fields returns a response without them, and a panel that throws
	 *  on undefined takes the WHOLE tenant page down with it, not just itself. */
	/** "at 00:00 Europe/Madrid (in 5h 12m)" — a time, not "eventually". */
	function resetText(b: RunBudget): string {
		if (!b.daily_resets_at) return '';
		const at = new Date(b.daily_resets_at);
		if (Number.isNaN(at.getTime())) return '';
		const mins = Math.max(0, Math.round((at.getTime() - Date.now()) / 60000));
		const h = Math.floor(mins / 60);
		const rel = h > 0 ? `in ${h}h ${mins % 60}m` : `in ${mins}m`;
		return `resets at midnight ${b.daily_timezone} (${rel})`;
	}

	function money(v: number | null | undefined): string {
		if (v == null || !Number.isFinite(v)) return '—';
		if (v === 0) return '$0.00';
		return Math.abs(v) < 0.01 ? `$${v.toFixed(6)}` : `$${v.toFixed(2)}`;
	}

	/** Same defence for the token figures. */
	function num(v: number | null | undefined): string {
		return v == null || !Number.isFinite(v) ? '—' : formatNumber(v);
	}

	function syncInputs(b: RunBudget) {
		overrideInput = b.tenant_override != null ? String(b.tenant_override) : '';
		dollarInput = b.dollar_tenant_override != null ? String(b.dollar_tenant_override) : '';
		dailyTokenInput = b.daily_token_override != null ? String(b.daily_token_override) : '';
		dailyDollarInput = b.daily_dollar_override != null ? String(b.daily_dollar_override) : '';
	}

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
			syncInputs(budget);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	/** Build the patch for one dimension, or throw a message for the user. */
	function tokenPatch(): number | null {
		const raw = overrideInput.trim();
		if (raw === '') return null; // clear the override
		const n = Number(raw);
		if (!Number.isInteger(n) || n < 1000) {
			throw new Error('Token budget must be a whole number of at least 1,000.');
		}
		if (budget && n > budget.install_max) {
			throw new Error(
				`Token budget must not exceed the install cap of ${formatNumber(budget.install_max)}.`
			);
		}
		return n;
	}

	function dollarPatch(): number | null {
		const raw = dollarInput.trim();
		if (raw === '') return null;
		const n = Number(raw);
		if (!Number.isFinite(n) || n <= 0) {
			throw new Error('Dollar budget must be a number greater than zero.');
		}
		if (budget && n > budget.dollar_install_max) {
			throw new Error(
				`Dollar budget must not exceed the install cap of ${money(budget.dollar_install_max)}.`
			);
		}
		return n;
	}

	/** What the loaded budget says each field should currently read as. */
	function pristine(b: RunBudget) {
		return {
			token: b.tenant_override != null ? String(b.tenant_override) : '',
			dollar: b.dollar_tenant_override != null ? String(b.dollar_tenant_override) : '',
			dailyToken: b.daily_token_override != null ? String(b.daily_token_override) : '',
			dailyDollar: b.daily_dollar_override != null ? String(b.daily_dollar_override) : ''
		};
	}

	/** Shared validation for the two 24h ceilings. */
	function dailyPatch(raw: string, max: number, whole: boolean, label: string): number | null {
		const v = raw.trim();
		if (v === '') return null;
		const n = Number(v);
		if (!Number.isFinite(n) || n <= 0 || (whole && !Number.isInteger(n))) {
			throw new Error(`${label} must be a ${whole ? 'whole number' : 'number'} greater than zero.`);
		}
		if (n > max) {
			throw new Error(`${label} must not exceed the install cap of ${whole ? formatNumber(max) : money(max)}.`);
		}
		return n;
	}

	async function save() {
		if (!editable || !budget) return;
		// Send ONLY the fields the operator actually changed. Sending both every
		// time defeats the API's tri-state contract: a blank token box that was
		// simply never touched would arrive as an explicit null and clear an
		// override another admin had just set (Codex review, finding 4).
		const was = pristine(budget);
		const patch: {
			token_override?: number | null;
			dollar_override?: number | null;
			daily_token_override?: number | null;
			daily_dollar_override?: number | null;
		} = {};
		try {
			if (overrideInput.trim() !== was.token) patch.token_override = tokenPatch();
			if (dollarInput.trim() !== was.dollar) patch.dollar_override = dollarPatch();
			if (dailyTokenInput.trim() !== was.dailyToken) {
				patch.daily_token_override = dailyPatch(
					dailyTokenInput, budget.daily_token_max, true, 'Daily token ceiling'
				);
			}
			if (dailyDollarInput.trim() !== was.dailyDollar) {
				patch.daily_dollar_override = dailyPatch(
					dailyDollarInput, budget.daily_dollar_max, false, 'Daily spend ceiling'
				);
			}
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
			return;
		}
		if (Object.keys(patch).length === 0) {
			error = 'Nothing changed.';
			return;
		}
		saving = true;
		error = null;
		try {
			budget = await api.runBudget.update(tenantId as string, patch);
			syncInputs(budget);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			saving = false;
		}
	}

	async function clearOverrides() {
		// Explicit intent, so both nulls are sent deliberately rather than as a
		// side effect of blank inputs.
		if (!editable || !budget) return;
		saving = true;
		error = null;
		try {
			budget = await api.runBudget.update(tenantId as string, {
				token_override: null,
				dollar_override: null,
				daily_token_override: null,
				daily_dollar_override: null
			});
			syncInputs(budget);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			saving = false;
		}
	}

	onMount(load);
</script>

<section class="card p-4" data-testid="run-budget-panel">
	<header class="flex items-center justify-between mb-3">
		<h3 class="h4">Agent Run Budget</h3>
		{#if budget && !loading}
			<div class="flex gap-2">
				<span class="badge variant-soft" data-testid="run-budget-effective">
					{num(budget.effective)} tokens/run
				</span>
				<span class="badge variant-soft" data-testid="run-budget-dollar-effective">
					{money(budget.dollar_effective)}/run
				</span>
			</div>
		{/if}
	</header>

	{#if loading}
		<div class="flex justify-center p-6">
			<div class="animate-spin w-5 h-5 border-2 border-primary-500 border-t-transparent rounded-full"></div>
		</div>
	{:else if error && !budget}
		<aside class="alert variant-soft-error"><p>{error}</p></aside>
	{:else if budget}
		{#if budget.daily_cap_hit}
			<aside class="alert variant-filled-warning mb-3" data-testid="run-budget-cap-hit">
				<div>
					<h4 class="font-semibold">Daily ceiling reached — new runs are not being claimed</h4>
					<p class="text-sm">
						{budget.daily_cap_reason}. Triage resumes on its own when the day
						rolls over — {resetText(budget)} — or raise the ceiling now.
					</p>
				</div>
			</aside>
		{/if}

		<dl class="grid grid-cols-2 gap-3 text-sm mb-4">
			<div>
				<dt class="opacity-60">Install default</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-default">
					{num(budget.install_default)} tokens · {money(budget.dollar_install_default)}
				</dd>
			</div>
			<div>
				<dt class="opacity-60">Install cap</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-cap">
					{num(budget.install_max)} tokens · {money(budget.dollar_install_max)}
				</dd>
			</div>
			<div>
				<dt class="opacity-60">Tenant override</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-override">
					{budget.tenant_override != null ? num(budget.tenant_override) : '—'} tokens ·
					{budget.dollar_tenant_override != null ? money(budget.dollar_tenant_override) : '—'}
				</dd>
			</div>
			<div>
				<dt class="opacity-60">Used (24h)</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-spend">
					{num(budget.spend_24h_tokens)} tokens · {money(budget.spend_24h_dollars)}
				</dd>
			</div>
			<div>
				<dt class="opacity-60">Daily ceiling <span class="opacity-60 text-xs">({budget.daily_timezone})</span></dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-daily-cap">
					{num(budget.daily_token_cap)} tokens · {money(budget.daily_dollar_cap)}
				</dd>
			</div>
			<div>
				<dt class="opacity-60" title={resetText(budget)}>Remaining today</dt>
				<dd class="font-mono tabular-nums" data-testid="run-budget-daily-remaining">
					{num(budget.daily_tokens_remaining)} tokens ·
					{money(budget.daily_dollars_remaining)}
				</dd>
			</div>
		</dl>

		{#if editable}
			<div class="flex items-end gap-2 flex-wrap">
				<label class="label flex-1 min-w-40">
					<span class="text-sm opacity-70">Tokens/run (blank = install default)</span>
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
				<label class="label flex-1 min-w-40">
					<span class="text-sm opacity-70">Dollars/run (blank = install default)</span>
					<input
						class="input font-mono"
						type="text"
						inputmode="decimal"
						placeholder={String(budget.dollar_install_default)}
						bind:value={dollarInput}
						data-testid="run-budget-dollar-input"
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
					on:click={clearOverrides}
					disabled={saving ||
						(budget.tenant_override == null &&
							budget.dollar_tenant_override == null &&
							budget.daily_token_override == null &&
							budget.daily_dollar_override == null)}
					data-testid="run-budget-clear"
				>
					Clear
				</button>
			</div>
			<div class="flex items-end gap-2 flex-wrap mt-3">
				<label class="label flex-1 min-w-40">
					<span class="text-sm opacity-70">
						Tokens/day (blank = {num(budget.daily_token_install_default)})
					</span>
					<input
						class="input font-mono"
						type="text"
						inputmode="numeric"
						placeholder={String(budget.daily_token_install_default)}
						bind:value={dailyTokenInput}
						data-testid="run-budget-daily-token-input"
						disabled={saving}
					/>
				</label>
				<label class="label flex-1 min-w-40">
					<span class="text-sm opacity-70">
						Dollars/day (blank = {money(budget.daily_dollar_install_default)})
					</span>
					<input
						class="input font-mono"
						type="text"
						inputmode="decimal"
						placeholder={String(budget.daily_dollar_install_default)}
						bind:value={dailyDollarInput}
						data-testid="run-budget-daily-dollar-input"
						disabled={saving}
					/>
				</label>
			</div>
			{#if error}
				<p class="text-error-500 text-sm mt-2" data-testid="run-budget-error">{error}</p>
			{/if}
			<p class="text-xs opacity-50 mt-2">
				Applies to the next run. In-flight runs keep their budget. Neither value may
				exceed the install cap.
			</p>
		{:else}
			<p class="text-xs opacity-50">
				The effective budget is set by your provider. New runs use it; in-flight runs keep theirs.
			</p>
		{/if}
	{/if}
</section>
