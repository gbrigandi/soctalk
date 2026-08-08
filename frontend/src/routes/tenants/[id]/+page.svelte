<script lang="ts">
	import { page } from '$app/stores';
	import { localizedGoto } from '$lib/i18n';
	import {
		tenantsApi,
		tenantStateBadge,
		type Tenant,
		type LifecycleEvent
	} from '$lib/api/tenants';
	import { addToast, authSession, canEditSettings, isMsspScope } from '$lib/stores';
	import ExternalSiemPanel from '$lib/components/tenants/ExternalSiemPanel.svelte';
	import LlmConfigPanel from '$lib/components/tenants/LlmConfigPanel.svelte';
	import RunBudgetPanel from '$lib/components/tenants/RunBudgetPanel.svelte';
	import ModelPricesPanel from '$lib/components/tenants/ModelPricesPanel.svelte';
	import { m } from '$lib/paraglide/messages';

	let tenant: Tenant | null = null;
	let events: LifecycleEvent[] = [];
	let loading = true;
	let error: string | null = null;
	let loadedFor: string | null = null;

	// $page.params.id is typed ``string | undefined``; normalize to a concrete
	// string so every tenantsApi.*(id) call (load + lifecycle actions) type-checks
	// and never forwards ``undefined``. The reactive load + the action buttons are
	// only reachable once a non-empty id has resolved (guarded below / gated on a
	// loaded ``tenant``), so the '' fallback is an inert placeholder.
	$: id = $page.params.id ?? '';

	$: if ($authSession.user && id) {
		if (!$isMsspScope) {
			localizedGoto('/');
		} else if (loadedFor !== id) {
			loadedFor = id;
			void load();
		}
	}

	async function load() {
		loading = true;
		error = null;
		try {
			[tenant, events] = await Promise.all([
				tenantsApi.get(id),
				tenantsApi.events(id, 50)
			]);
		} catch (e) {
			error = e instanceof Error ? e.message : m.ten_load_one_failed();
		} finally {
			loading = false;
		}
	}

	// ``label`` is a message-function REF (called at toast time) — never
	// evaluate messages at module scope (#52).
	async function act(fn: () => Promise<unknown>, label: () => string) {
		try {
			await fn();
			addToast({ type: 'success', title: m.scope_tenant(), message: m.ten_action_ok({ action: label() }) });
			await load();
		} catch (e) {
			addToast({
				type: 'error',
				title: label(),
				message: e instanceof Error ? e.message : String(e)
			});
		}
	}

	function fmtDate(ts: string): string {
		try {
			return new Date(ts).toLocaleString();
		} catch {
			return ts;
		}
	}
</script>

<div class="space-y-4">
	<div class="flex items-center gap-3">
		<button class="btn btn-sm variant-ghost-surface" on:click={() => localizedGoto('/tenants')}>
			{m.ten_back_to_tenants()}
		</button>
	</div>

	{#if loading}
		<div class="card p-6 flex items-center gap-3">
			<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current"></span>
			<span>{m.common_loading()}</span>
		</div>
	{:else if error}
		<div class="card p-6 text-error-500">{error}</div>
	{:else if tenant}
		<div class="flex items-baseline justify-between">
			<div>
				<h1 class="h2">{tenant.display_name}</h1>
				<p class="text-sm opacity-70 font-mono">{tenant.slug}</p>
			</div>
			<span class="badge {tenantStateBadge(tenant.state)} text-base" data-testid="tenant-state" data-state={tenant.state}>{tenant.state}</span>
		</div>

		<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
			<div class="card p-4">
				<h3 class="h4 mb-4">{m.ten_identity()}</h3>
				<dl class="space-y-2 text-sm">
					<div class="flex justify-between">
						<dt class="opacity-60">{m.ten_id_label()}</dt>
						<dd class="font-mono text-xs">{tenant.id}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="opacity-60">{m.ten_profile()}</dt>
						<dd>{tenant.profile ?? '—'}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="opacity-60">{m.ten_created()}</dt>
						<dd>{fmtDate(tenant.created_at)}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="opacity-60">{m.ten_state_changed()}</dt>
						<dd>{fmtDate(tenant.state_changed_at)}</dd>
					</div>
				</dl>
			</div>

			<div class="card p-4 lg:col-span-2">
				<h3 class="h4 mb-4">{m.ten_actions()}</h3>
				<div class="flex flex-wrap gap-2">
					<button
						class="btn btn-sm variant-filled-warning"
						disabled={tenant.state !== 'active'}
						on:click={() => act(() => tenantsApi.suspend(id), m.ten_action_suspend)}
					>
						{m.ten_suspend()}
					</button>
					<button
						class="btn btn-sm variant-filled-success"
						disabled={tenant.state !== 'suspended'}
						on:click={() => act(() => tenantsApi.resume(id), m.ten_action_resume)}
					>
						{m.ten_resume()}
					</button>
					<button
						class="btn btn-sm variant-filled-secondary"
						disabled={!['pending', 'degraded'].includes(tenant.state)}
						on:click={() => act(() => tenantsApi.retry(id), m.ten_action_retry_provisioning)}
					>
						{m.ten_retry_provisioning()}
					</button>
					<button
						class="btn btn-sm variant-filled-error"
						disabled={['decommissioning', 'archived', 'purged'].includes(tenant.state)}
						on:click={() => act(() => tenantsApi.decommission(id), m.ten_action_decommission)}
					>
						{m.ten_decommission()}
					</button>
				</div>
			</div>
		</div>

		<!-- External SIEM connection + live adapter status. Only relevant for
		     the 'provided' profile (BYO Wazuh) — for 'poc' / 'persistent' the
		     chart installs Wazuh in-cluster and the panel would surface an
		     in-namespace svc URL plus a confusing 'unreachable' ingest status.
		     Keyed by id so switching tenant remounts (fresh fetch + poll cycle). -->
		{#key id}
			{#if tenant.profile === 'provided'}
				<ExternalSiemPanel tenantId={id} />
			{/if}
			<!-- Per-tenant LLM config (masked key). Shown for ANY profile; same
			     keyed block so switching tenant remounts (fresh fetch). -->
			<LlmConfigPanel tenantId={id} />
			<!-- Per-tenant Agent Run token budget (#103): DB-resolved override,
			     capped at the install max, no worker rollout. -->
			<RunBudgetPanel tenantId={id} canEdit={$canEditSettings} />
			<!-- What this tenant is billed per token (#121 overlay over the #125
			     catalog), and the rates a new run would be stamped with. -->
			<ModelPricesPanel tenantId={id} canEdit={$canEditSettings} />
		{/key}

		<div class="card p-4">
			<h3 class="h4 mb-4">{m.ten_lifecycle_events()}</h3>
			{#if events.length === 0}
				<p class="opacity-70 text-sm">{m.ten_no_events()}</p>
			{:else}
				<table class="table table-compact">
					<thead>
						<tr>
							<th>{m.ten_th_time()}</th>
							<th>{m.ten_th_event()}</th>
							<th>{m.ten_th_from()}</th>
							<th>{m.ten_th_to()}</th>
						</tr>
					</thead>
					<tbody>
						{#each events as e (e.id)}
							<tr>
								<td class="text-xs opacity-70">{fmtDate(e.timestamp)}</td>
								<td><code class="text-xs">{e.event_type}</code></td>
								<td>{e.from_state ?? '—'}</td>
								<td>{e.to_state ?? '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</div>
	{/if}
</div>
