<script lang="ts">
	import { api, type AuthoredResponsePlaybook } from '$lib/api/client';
	import { authSession, currentTenantId, canManageTriagePolicies, isMsspUser } from '$lib/stores';
	import { m } from '$lib/paraglide/messages';
	import { localizeHref, localizedGoto } from '$lib/i18n';

	// MSSP-side (/api/mssp/*) page. The nav already hides it from tenant
	// audiences; a tenant user who reaches it by URL is denied by the role
	// wall, so bounce them rather than surface a raw permission error, and
	// gate the fetch on the audience so no 403 fires before the bounce.
	$: if ($authSession.user && !$isMsspUser) {
		localizedGoto('/');
	}

	// Reuses the admin-tier config-management gate (the API gates response-playbook
	// mutations with the same MSSP_ADMIN role as triage policies).
	$: canManage = $canManageTriagePolicies;

	let authored: AuthoredResponsePlaybook[] = [];
	let loading = false;
	let error: string | null = null;
	let note: string | null = null;

	let editorOpen = false;
	let editorMode: 'create' | 'edit' = 'create';
	let editorPid = '';
	let editorText = '';
	let editorSaving = false;
	let editorError: string | null = null;

	$: tenantId = $currentTenantId;
	$: if (tenantId && $authSession.user && $isMsspUser) load(tenantId);

	async function load(tid: string) {
		loading = true;
		error = null;
		try {
			authored = await api.responsePlaybooks.listAuthored(tid);
		} catch (e) {
			error = e instanceof Error ? e.message : m.rp_load_failed();
		} finally {
			loading = false;
		}
	}

	function openCreate() {
		editorMode = 'create';
		editorPid = '';
		editorError = null;
		editorText = JSON.stringify(
			{
				id: 'my-response-playbook',
				version: 1,
				applies_to: { rule_groups: [] },
				response: {
					on_escalate: [
						{ capability: 'annotate_investigation', params: { body: 'escalation acknowledged' } }
					]
				}
			},
			null,
			2
		);
		editorOpen = true;
	}

	function openEdit(pb: AuthoredResponsePlaybook) {
		editorMode = 'edit';
		editorPid = pb.response_playbook_id;
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
			editorError = m.rp_invalid_json();
			return;
		}
		editorSaving = true;
		editorError = null;
		try {
			if (editorMode === 'create') await api.responsePlaybooks.createAuthored(tenantId, def);
			else await api.responsePlaybooks.updateAuthored(tenantId, editorPid, def);
			editorOpen = false;
			await load(tenantId);
		} catch (e) {
			editorError = e instanceof Error ? e.message : m.rp_save_failed();
		} finally {
			editorSaving = false;
		}
	}

	async function retire(pid: string) {
		if (!tenantId || !confirm(m.rp_confirm_delete({ id: pid })))
			return;
		try {
			await api.responsePlaybooks.retireAuthored(tenantId, pid);
			await load(tenantId);
		} catch (e) {
			error = e instanceof Error ? e.message : m.rp_delete_failed();
		}
	}

	async function exportYaml(pid: string) {
		if (!tenantId) return;
		try {
			const res = await api.responsePlaybooks.exportAuthored(tenantId, pid);
			const blob = new Blob([res.yaml], { type: 'text/yaml' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `${pid}.yaml`;
			a.click();
			URL.revokeObjectURL(url);
		} catch (e) {
			error = e instanceof Error ? e.message : m.rp_export_failed();
		}
	}

	async function setActive(pid: string, active: boolean) {
		if (!tenantId) return;
		error = null;
		try {
			if (active) await api.responsePlaybooks.activateAuthored(tenantId, pid);
			else await api.responsePlaybooks.deactivateAuthored(tenantId, pid);
			// Unlike triage policies, activation is LIVE — L1 dispatches from the DB,
			// so there is no worker rollout to wait for.
			note = active ? m.rp_note_active({ id: pid }) : m.rp_note_shadow({ id: pid });
			await load(tenantId);
		} catch (e) {
			error = e instanceof Error ? e.message : m.rp_activation_failed();
		}
	}

	function statusBadge(s: string): string {
		return s === 'active'
			? 'variant-filled-success'
			: s === 'shadow'
				? 'variant-soft-warning'
				: 'variant-soft';
	}

	function actionSummary(pb: AuthoredResponsePlaybook): string {
		const r = (pb.definition?.response ?? {}) as Record<string, unknown>;
		const caps = (phase: string): string[] =>
			(((r[phase] as unknown[]) ?? []) as Record<string, unknown>[]).map((a) =>
				String(a.capability ?? '?')
			);
		const parts: string[] = [];
		const esc = caps('on_escalate');
		const cls = caps('on_close');
		if (esc.length) parts.push(m.rp_summary_escalate({ caps: esc.join(', ') }));
		if (cls.length) parts.push(m.rp_summary_close({ caps: cls.join(', ') }));
		return parts.join('  ·  ') || '—';
	}
</script>

<svelte:head>
	<title>{m.rp_title()} - SocTalk</title>
</svelte:head>

<div class="flex items-center justify-between mb-2">
	<h1 class="h2">{m.rp_title()}</h1>
	{#if tenantId && canManage}
		<div class="flex gap-2">
			<a class="btn btn-sm variant-filled-primary" href={localizeHref('/response-playbooks/editor')}>{m.rp_new()}</a>
			<button class="btn btn-sm variant-soft" on:click={openCreate} title={m.rp_json_title()}>JSON</button>
		</div>
	{/if}
</div>
<p class="opacity-60 text-sm mb-6">{m.rp_intro()}</p>

{#if !tenantId}
	<div class="card p-6 opacity-60 text-sm">{m.rp_pin_hint()}</div>
{:else}
	{#if error}
		<div class="alert variant-filled-error mb-3"><span>{error}</span></div>
	{/if}
	{#if note}
		<div class="alert variant-soft-primary mb-3 text-sm"><span>{note}</span></div>
	{/if}
	{#if loading}
		<div class="card p-6 text-center opacity-60 text-sm">{m.common_loading()}</div>
	{:else if authored.length === 0}
		<div class="card p-6 opacity-60 text-sm">{m.rp_empty()}</div>
	{:else}
		<div class="grid gap-2">
			{#each authored as pb (pb.response_playbook_id)}
				<div class="card p-4 flex items-center justify-between gap-3">
					<div class="flex flex-col min-w-0 gap-1">
						<div class="flex items-center gap-2 min-w-0">
							<span class="font-mono font-semibold truncate">{pb.response_playbook_id}</span>
							<span class="badge {statusBadge(pb.status)} text-xs">{pb.status}</span>
							<span class="badge variant-soft text-xs">{m.rp_rev({ rev: pb.revision })}</span>
						</div>
						<div class="text-xs opacity-60 truncate">{actionSummary(pb)}</div>
					</div>
					<div class="flex items-center gap-2 flex-shrink-0">
						<button class="btn btn-sm variant-soft" on:click={() => exportYaml(pb.response_playbook_id)}>
							{m.common_export()}
						</button>
						{#if canManage}
							{#if pb.status === 'active'}
								<button
									class="btn btn-sm variant-soft"
									on:click={() => setActive(pb.response_playbook_id, false)}
								>
									{m.rp_deactivate()}
								</button>
							{:else}
								<button
									class="btn btn-sm variant-filled-success"
									on:click={() => setActive(pb.response_playbook_id, true)}
								>
									{m.rp_activate()}
								</button>
							{/if}
							<a
								class="btn btn-sm variant-filled-primary"
								href={localizeHref(`/response-playbooks/editor?id=${encodeURIComponent(pb.response_playbook_id)}`)}
							>
								{m.common_edit()}
							</a>
							<button class="btn btn-sm variant-soft" on:click={() => openEdit(pb)} title={m.rp_json_title()}>
								JSON
							</button>
							<button
								class="btn btn-sm variant-soft-error"
								on:click={() => retire(pb.response_playbook_id)}
							>
								{m.common_delete()}
							</button>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{/if}
{/if}

{#if editorOpen}
	<div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
		<div class="card p-6 max-w-2xl w-full space-y-4">
			<h3 class="h4">
				{editorMode === 'create' ? m.rp_modal_new_title() : m.rp_modal_edit_title({ id: editorPid })}
			</h3>
			<p class="text-xs opacity-60">{m.rp_modal_hint()}</p>
			<textarea class="textarea font-mono text-xs h-80" bind:value={editorText}></textarea>
			{#if editorError}
				<div class="alert variant-filled-error text-sm"><span>{editorError}</span></div>
			{/if}
			<div class="flex justify-end gap-2">
				<button class="btn variant-soft" on:click={() => (editorOpen = false)} disabled={editorSaving}>
					{m.common_cancel()}
				</button>
				<button class="btn variant-filled-primary" on:click={save} disabled={editorSaving}>
					{editorSaving ? m.common_saving() : m.common_save()}
				</button>
			</div>
		</div>
	</div>
{/if}
