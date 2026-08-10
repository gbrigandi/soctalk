<script lang="ts">
	import { api, type TriagePolicy, type AuthoredTriagePolicy } from '$lib/api/client';
	import { m } from '$lib/paraglide/messages';
	import { localizeHref, localizedGoto } from '$lib/i18n';
	import { authSession, currentTenantId, canManageTriagePolicies, isMsspUser } from '$lib/stores';

	let policies: TriagePolicy[] = [];
	let loading = true;
	let error: string | null = null;
	let expanded = new Set<string>();

	// --- authored (per-tenant, shadow/draft) ---
	let authored: AuthoredTriagePolicy[] = [];
	let authoredLoading = false;
	let authoredError: string | null = null;
	let editorOpen = false;
	let editorMode: 'create' | 'edit' = 'create';
	let editorPid = '';
	let editorText = '';
	let editorSaving = false;
	let editorError: string | null = null;

	// MSSP-side (/api/mssp/*) page. The nav already hides it from tenant
	// audiences; a tenant user who reaches it by URL is denied by the role
	// wall, so bounce them rather than surface a raw permission error, and
	// gate every fetch on the audience so no 403 fires before the bounce.
	$: if ($authSession.user && !$isMsspUser) {
		localizedGoto('/');
	}

	$: tenantId = $currentTenantId;
	$: if (tenantId && $authSession.user && $isMsspUser) loadAuthored(tenantId);

	async function loadAuthored(tid: string) {
		authoredLoading = true;
		authoredError = null;
		try {
			authored = await api.triagePolicies.listAuthored(tid);
		} catch (e) {
			authoredError = e instanceof Error ? e.message : m.tp_load_authored_failed();
		} finally {
			authoredLoading = false;
		}
	}

	function openCreate() {
		editorMode = 'create';
		editorPid = '';
		editorError = null;
		editorText = JSON.stringify(
			{ id: 'my-triage-policy', priority: 70, applies_to: { rule_groups: [] }, guardrails: [] },
			null,
			2
		);
		editorOpen = true;
	}

	function openEdit(pb: AuthoredTriagePolicy) {
		editorMode = 'edit';
		editorPid = pb.triage_policy_id;
		editorError = null;
		editorText = JSON.stringify(pb.definition, null, 2);
		editorOpen = true;
	}

	async function save() {
		if (!tenantId) return;
		let def: Record<string, unknown>;
		try {
			def = JSON.parse(editorText);
		} catch {
			editorError = m.tp_invalid_json();
			return;
		}
		editorSaving = true;
		editorError = null;
		try {
			if (editorMode === 'create') await api.triagePolicies.createAuthored(tenantId, def);
			else await api.triagePolicies.updateAuthored(tenantId, editorPid, def);
			editorOpen = false;
			await loadAuthored(tenantId);
		} catch (e) {
			editorError = e instanceof Error ? e.message : m.tp_save_failed();
		} finally {
			editorSaving = false;
		}
	}

	async function retire(pid: string) {
		if (!tenantId || !confirm(m.tp_confirm_delete({ id: pid })))
			return;
		try {
			await api.triagePolicies.retireAuthored(tenantId, pid);
			await loadAuthored(tenantId);
		} catch (e) {
			authoredError = e instanceof Error ? e.message : m.tp_delete_failed();
		}
	}

	async function exportYaml(pid: string) {
		if (!tenantId) return;
		try {
			const res = await api.triagePolicies.exportAuthored(tenantId, pid);
			const blob = new Blob([res.yaml], { type: 'text/yaml' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `${pid}.yaml`;
			a.click();
			URL.revokeObjectURL(url);
		} catch (e) {
			authoredError = e instanceof Error ? e.message : m.tp_export_failed();
		}
	}

	let rolloutNote: string | null = null;

	async function setActive(pid: string, active: boolean) {
		if (!tenantId) return;
		authoredError = null;
		try {
			if (active) await api.triagePolicies.activateAuthored(tenantId, pid);
			else await api.triagePolicies.deactivateAuthored(tenantId, pid);
			rolloutNote = active
				? m.tp_rollout_activating({ id: pid })
				: m.tp_rollout_deactivating({ id: pid });
			await loadAuthored(tenantId);
		} catch (e) {
			authoredError = e instanceof Error ? e.message : m.tp_activation_change_failed();
		}
	}

	function authoredStatusBadge(s: string): string {
		return s === 'active'
			? 'variant-filled-success'
			: s === 'shadow'
				? 'variant-soft-warning'
				: 'variant-soft';
	}

	// Session-gated one-shot instead of onMount: the session may hydrate
	// after mount, and the built-in list is MSSP-only too.
	let builtinRequested = false;
	$: if ($authSession.user && $isMsspUser && !builtinRequested) {
		builtinRequested = true;
		loadPolicies();
	}

	async function loadPolicies() {
		loading = true;
		error = null;
		try {
			policies = await api.triagePolicies.list();
		} catch (e) {
			error = e instanceof Error ? e.message : m.tp_load_failed();
		} finally {
			loading = false;
		}
	}

	function toggle(id: string) {
		expanded.has(id) ? expanded.delete(id) : expanded.add(id);
		expanded = expanded;
	}

	function statusBadge(status: string): string {
		return status === 'active' ? 'variant-filled-success' : 'variant-soft-warning';
	}

	function sourceBadge(source: string): string {
		return source === 'built-in' ? 'variant-soft' : 'variant-soft-primary';
	}

	function matchSummary(pb: TriagePolicy): string {
		const parts: string[] = [];
		const applies = pb.applies_to;
		if (applies.rule_groups.length) parts.push(m.tp_match_groups({ v: applies.rule_groups.join(', ') }));
		if (applies.rule_ids.length) parts.push(m.tp_match_rules({ v: applies.rule_ids.join(', ') }));
		if (applies.authorization_tracks.length)
			parts.push(m.tp_match_authz({ v: applies.authorization_tracks.join(', ') }));
		return parts.join(m.tp_match_separator()) || m.tp_empty_dash();
	}

	$: activeCount = policies.filter((p) => p.status === 'active').length;
	$: shadowCount = policies.filter((p) => p.status === 'shadow').length;
</script>

<svelte:head>
	<title>{m.tp_title()} - SocTalk</title>
</svelte:head>

<div class="flex items-center justify-between mb-2">
	<h1 class="h2">{m.tp_title()}</h1>
	<button class="btn variant-soft btn-sm" on:click={loadPolicies} disabled={loading}>
		{#if loading}
			<span
				class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"
			></span>
		{/if}
		{m.tp_refresh()}
	</button>
</div>
<p class="opacity-60 text-sm mb-6">
	{m.tp_intro()}
</p>

{#if loading}
	<div class="flex items-center justify-center h-64">
		<div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500"></div>
	</div>
{:else if error}
	<div class="alert variant-filled-error"><span>{m.tp_error_prefix({ error })}</span></div>
{:else if policies.length === 0}
	<div class="card p-8 text-center opacity-60">{m.tp_no_policies_configured()}</div>
{:else}
	<div class="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
		<div class="card p-3">
			<h4 class="text-xs opacity-60 uppercase tracking-wide">{m.tp_total()}</h4>
			<p class="text-2xl font-bold">{policies.length}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60 uppercase tracking-wide">{m.tp_active()}</h4>
			<p class="text-2xl font-bold text-success-500">{activeCount}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60 uppercase tracking-wide">{m.tp_shadow()}</h4>
			<p class="text-2xl font-bold text-warning-500">{shadowCount}</p>
		</div>
	</div>

	<div class="grid gap-3">
		{#each policies as pb (pb.id)}
			{@const isOpen = expanded.has(pb.id)}
			<div class="card">
				<button
					class="w-full p-4 text-left hover:bg-surface-500/5 transition-colors"
					on:click={() => toggle(pb.id)}
				>
					<div class="flex flex-col lg:flex-row lg:items-center gap-3">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							class="h-5 w-5 opacity-60 transition-transform flex-shrink-0 {isOpen
								? 'rotate-90'
								: ''}"
							fill="none"
							viewBox="0 0 24 24"
							stroke="currentColor"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M9 5l7 7-7 7"
							/>
						</svg>
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2 flex-wrap">
								<span class="font-semibold font-mono truncate">{pb.id}</span>
								<span class="badge {statusBadge(pb.status)} text-xs">{pb.status}</span>
								<span class="badge {sourceBadge(pb.source)} text-xs">{pb.source}</span>
							</div>
							<div class="text-xs opacity-60 mt-1 truncate">{matchSummary(pb)}</div>
						</div>
						<div class="flex items-center gap-3 text-xs opacity-60 flex-shrink-0">
							<span>{m.tp_version_short({ version: pb.version })}</span>
							<span>{m.tp_priority_summary({ priority: pb.priority })}</span>
						</div>
					</div>
				</button>

				{#if isOpen}
					<div class="border-t border-surface-500/20 p-4 space-y-4 text-sm">
						{#if pb.deterministic_disposition}
							<div>
								<span class="opacity-60">{m.tp_deterministic_disposition()}</span>
								<span class="badge variant-soft-error text-xs ml-1"
									>{pb.deterministic_disposition}</span
								>
								<span class="opacity-60 text-xs"
									>&nbsp;{m.tp_deterministic_disposition_note()}</span
								>
							</div>
						{/if}

						{#if pb.required_steps.length}
							<div>
								<span class="opacity-60">{m.tp_required_steps_before_verdict()}</span>
								{#each pb.required_steps as s}
									<span class="badge variant-soft text-xs ml-1 font-mono">{s}</span>
								{/each}
							</div>
						{/if}

						{#if pb.decision_modules.length}
							<div>
								<span class="opacity-60">{m.tp_decision_modules()}</span>
								{#each pb.decision_modules as d}
									<span class="badge variant-soft text-xs ml-1 font-mono">{d}</span>
								{/each}
							</div>
						{/if}

						{#if Object.keys(pb.legal_actions).length}
							<div>
								<span class="opacity-60">{m.tp_legal_actions_per_phase()}</span>
								<div class="mt-1 space-y-1">
									{#each Object.entries(pb.legal_actions) as [phase, actions]}
										<div class="flex gap-2 items-baseline flex-wrap">
											<span class="font-mono text-xs opacity-70 w-16">{phase}</span>
											{#each actions as a}
												<span class="badge variant-soft text-xs font-mono">{a}</span>
											{/each}
										</div>
									{/each}
								</div>
							</div>
						{/if}

						{#if pb.close_signoff_data_classes.length}
							<div>
								<span class="opacity-60">{m.tp_close_requires_signoff()}</span>
								{#each pb.close_signoff_data_classes as c}
									<span class="badge variant-soft-warning text-xs ml-1">{c}</span>
								{/each}
							</div>
						{/if}

						{#if pb.guardrails.length}
							<div>
								<span class="opacity-60">{m.tp_guardrails_label()}</span>
								<div class="mt-1 space-y-2">
									{#each pb.guardrails as g}
										<div class="card variant-soft p-3">
											<div class="flex items-center gap-2 flex-wrap">
												<span class="badge variant-filled-warning text-xs"
													>{m.tp_guardrail_effect_target({ effect: g.effect, target: g.to })}</span
												>
												<span class="text-xs opacity-80">{g.reason}</span>
											</div>
											<pre
												class="text-xs mt-2 overflow-x-auto opacity-70">{JSON.stringify(
													g.when,
													null,
													2
												)}</pre>
										</div>
									{/each}
								</div>
							</div>
						{/if}

						{#if !pb.required_steps.length && !pb.decision_modules.length && !pb.guardrails.length && !pb.deterministic_disposition && !Object.keys(pb.legal_actions).length && !pb.close_signoff_data_classes.length}
							<p class="opacity-60">{m.tp_matching_only()}</p>
						{/if}
					</div>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<!-- Authored triage policies (per-tenant, shadow/draft) -->
<div class="mt-10">
	<div class="flex items-center justify-between mb-2">
		<h2 class="h3">{m.tp_authored_title()}</h2>
		{#if tenantId && $canManageTriagePolicies}
			<div class="flex gap-2">
				<a class="btn btn-sm variant-filled-primary" href={localizeHref('/triage-policies/editor')}
					>{m.tp_new_policy()}</a
				>
				<button class="btn btn-sm variant-soft" on:click={openCreate} title={m.tp_raw_json_editor()}>
					{m.tp_json_button()}
				</button>
			</div>
		{/if}
	</div>

	{#if !tenantId}
		<div class="card p-6 opacity-60 text-sm">
			{m.tp_authored_pin_hint()}
		</div>
	{:else}
		<p class="opacity-60 text-sm mb-3">
			{m.tp_authored_intro()}
		</p>
		{#if authoredError}
			<div class="alert variant-filled-error mb-3"><span>{authoredError}</span></div>
		{/if}
		{#if rolloutNote}
			<div class="alert variant-soft-primary mb-3 text-sm"><span>{rolloutNote}</span></div>
		{/if}
		{#if authoredLoading}
			<div class="card p-6 text-center opacity-60 text-sm">{m.tp_loading()}</div>
		{:else if authored.length === 0}
			<div class="card p-6 opacity-60 text-sm">{m.tp_no_authored_policies()}</div>
		{:else}
			<div class="grid gap-2">
				{#each authored as pb (pb.triage_policy_id)}
					<div class="card p-4 flex items-center justify-between gap-3">
						<div class="flex items-center gap-2 min-w-0">
							<span class="font-mono font-semibold truncate">{pb.triage_policy_id}</span>
							<span class="badge {authoredStatusBadge(pb.status)} text-xs">{pb.status}</span>
							<span class="badge variant-soft text-xs">{m.tp_revision({ revision: pb.revision })}</span>
						</div>
						<div class="flex items-center gap-2 flex-shrink-0">
							<button class="btn btn-sm variant-soft" on:click={() => exportYaml(pb.triage_policy_id)}>
								{m.tp_export()}
							</button>
							{#if $canManageTriagePolicies}
								{#if pb.status === 'active'}
									<button
										class="btn btn-sm variant-soft"
										on:click={() => setActive(pb.triage_policy_id, false)}
									>
										{m.tp_deactivate()}
									</button>
								{:else}
									<button
										class="btn btn-sm variant-filled-success"
										on:click={() => setActive(pb.triage_policy_id, true)}
									>
										{m.tp_activate()}
									</button>
								{/if}
								<a
									class="btn btn-sm variant-filled-primary"
									href={localizeHref(
										`/triage-policies/editor?id=${encodeURIComponent(pb.triage_policy_id)}`
									)}
								>
									{m.tp_edit()}
								</a>
								<button
									class="btn btn-sm variant-soft"
									on:click={() => openEdit(pb)}
									title={m.tp_raw_json_editor()}
								>
									{m.tp_json_button()}
								</button>
								<button
									class="btn btn-sm variant-soft-error"
									on:click={() => retire(pb.triage_policy_id)}
								>
									{m.tp_delete()}
								</button>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</div>

{#if editorOpen}
	<div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
		<div class="card p-6 max-w-2xl w-full space-y-4">
			<h3 class="h4">
				{editorMode === 'create' ? m.tp_modal_new_title() : m.tp_modal_edit_title({ id: editorPid })}
			</h3>
			<p class="text-xs opacity-60">
				{m.tp_modal_hint()}
			</p>
			<textarea class="textarea font-mono text-xs h-80" bind:value={editorText}></textarea>
			{#if editorError}
				<div class="alert variant-filled-error text-sm"><span>{editorError}</span></div>
			{/if}
			<div class="flex justify-end gap-2">
				<button class="btn variant-soft" on:click={() => (editorOpen = false)} disabled={editorSaving}>
					{m.tp_cancel()}
				</button>
				<button class="btn variant-filled-primary" on:click={save} disabled={editorSaving}>
					{editorSaving ? m.tp_saving() : m.tp_save()}
				</button>
			</div>
		</div>
	</div>
{/if}
