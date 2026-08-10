<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api } from '$lib/api/client';
	import { m } from '$lib/paraglide/messages';
	import { localizeHref, localizedGoto } from '$lib/i18n';
	import { authSession, currentTenantId, isMsspUser } from '$lib/stores';
	import ResponseFlowPreview from '$lib/response-playbook/ResponseFlowPreview.svelte';
	import {
		CAPABILITIES,
		CAP_BY_NAME,
		capLabel,
		SCALAR_FIELDS,
		LIST_FIELDS,
		COMPARISONS,
		emptyWhen,
		rowToWhen,
		whenToRow,
		validateDefinition,
		type ResponseActionDef,
		type ResponsePlaybookDef,
		type WhenRow
	} from '$lib/response-playbook/schema';

	type Which = 'escalate' | 'close';

	// MSSP-side editor (same audience wall as /response-playbooks): bounce a
	// tenant user who direct-loads the URL, and key the load on the audience
	// so no MSSP-only fetch fires for them.
	$: if ($authSession.user && !$isMsspUser) {
		localizedGoto('/');
	}

	$: tenantId = $currentTenantId;
	$: editId = $page.url.searchParams.get('id');
	$: mode = editId ? 'edit' : ('create' as 'edit' | 'create');

	let loaded = false;
	let loadError: string | null = null;

	let pid = 'my-response-playbook';
	let version = 1;
	let priority = 100;
	let ruleGroupsText = '';
	let ruleIdsText = '';
	let mitreTechniquesText = '';
	let mitreTacticsText = '';
	let existingStatus: string | null = null;

	interface ActionState {
		capability: string;
		paramsText: string;
		paramsError: string | null;
		useWhen: boolean;
		when: WhenRow;
	}
	let onEscalate: ActionState[] = [];
	let onClose: ActionState[] = [];

	let saving = false;
	let saveError: string | null = null;

	const ALL_FIELDS = [...SCALAR_FIELDS, ...LIST_FIELDS];
	const LIST_FIELD_SET = new Set<string>(LIST_FIELDS);
	const isListField = (f: string): boolean => LIST_FIELD_SET.has(f);

	function csv(text: string): string[] {
		return text
			.split(',')
			.map((s) => s.trim())
			.filter((s) => s.length > 0);
	}

	function emptyAction(cap = 'annotate_investigation'): ActionState {
		return {
			capability: cap,
			paramsText:
				cap === 'annotate_investigation'
					? JSON.stringify({ body: m.rpe_default_note_body() }, null, 2)
					: '{}',
			paramsError: null,
			useWhen: false,
			when: emptyWhen()
		};
	}

	function actionParams(a: ActionState): Record<string, unknown> {
		try {
			const v = a.paramsText.trim() ? JSON.parse(a.paramsText) : {};
			a.paramsError = null;
			return v;
		} catch {
			a.paramsError = m.rpe_params_invalid();
			return {};
		}
	}

	function toDef(list: ActionState[]): ResponseActionDef[] {
		return list.map((a) => {
			const out: ResponseActionDef = { capability: a.capability };
			const w = a.useWhen ? rowToWhen(a.when) : null;
			if (w) out.when = w;
			const p = actionParams(a);
			if (Object.keys(p).length) out.params = p;
			return out;
		});
	}

	$: definition = ((): ResponsePlaybookDef => {
		const def: ResponsePlaybookDef = { id: pid };
		if (version !== 1) def.version = version;
		if (priority !== 100) def.priority = priority;
		const applies: NonNullable<ResponsePlaybookDef['applies_to']> = {};
		if (csv(ruleGroupsText).length) applies.rule_groups = csv(ruleGroupsText);
		if (csv(ruleIdsText).length) applies.rule_ids = csv(ruleIdsText);
		if (csv(mitreTechniquesText).length) applies.mitre_techniques = csv(mitreTechniquesText);
		if (csv(mitreTacticsText).length) applies.mitre_tactics = csv(mitreTacticsText);
		if (Object.keys(applies).length) def.applies_to = applies;
		const resp: NonNullable<ResponsePlaybookDef['response']> = {};
		if (onEscalate.length) resp.on_escalate = toDef(onEscalate);
		if (onClose.length) resp.on_close = toDef(onClose);
		if (Object.keys(resp).length) def.response = resp;
		return def;
	})();

	$: paramErrors = [...onEscalate, ...onClose].some((a) => a.paramsError);
	$: validationErrors = [
		...validateDefinition(definition),
		...(paramErrors ? [m.verr_fix_json()] : [])
	];

	function addAction(which: 'escalate' | 'close') {
		if (which === 'escalate') onEscalate = [...onEscalate, emptyAction()];
		else onClose = [...onClose, emptyAction('annotate_investigation')];
	}
	function removeAction(which: 'escalate' | 'close', i: number) {
		if (which === 'escalate') onEscalate = onEscalate.filter((_, idx) => idx !== i);
		else onClose = onClose.filter((_, idx) => idx !== i);
	}

	function loadDefinition(def: Record<string, unknown>) {
		pid = String(def.id ?? '');
		version = Number(def.version ?? 1);
		priority = Number(def.priority ?? 100);
		const applies = (def.applies_to ?? {}) as Record<string, string[]>;
		ruleGroupsText = (applies.rule_groups ?? []).join(', ');
		ruleIdsText = (applies.rule_ids ?? []).join(', ');
		mitreTechniquesText = (applies.mitre_techniques ?? []).join(', ');
		mitreTacticsText = (applies.mitre_tactics ?? []).join(', ');
		const resp = (def.response ?? {}) as Record<string, unknown[]>;
		const fromDef = (arr: unknown[] | undefined): ActionState[] =>
			((arr ?? []) as Record<string, unknown>[]).map((a) => {
				const row = whenToRow(a.when);
				return {
					capability: String(a.capability ?? 'annotate_investigation'),
					paramsText: JSON.stringify(a.params ?? {}, null, 2),
					paramsError: null,
					useWhen: !!a.when,
					when: row ?? emptyWhen()
				};
			});
		onEscalate = fromDef(resp.on_escalate);
		onClose = fromDef(resp.on_close);
	}

	onMount(() => {
		if (!editId) {
			onEscalate = [emptyAction()];
			loaded = true;
		}
	});

	let loadedKey = '';
	$: loadKey =
		editId && tenantId && $authSession.user && $isMsspUser ? `${tenantId}|${editId}` : '';
	$: if (loadKey && loadKey !== loadedKey) {
		loadedKey = loadKey;
		loaded = false;
		loadError = null;
		loadExisting(tenantId!, editId!, loadKey);
	}

	async function loadExisting(tid: string, id: string, key: string) {
		try {
			const all = await api.responsePlaybooks.listAuthored(tid);
			if (key !== loadedKey) return;
			const row = all.find((p) => p.response_playbook_id === id);
			if (!row) throw new Error(m.rpe_not_found({ id }));
			existingStatus = row.status;
			loadDefinition(row.definition);
			loaded = true;
		} catch (e) {
			if (key !== loadedKey) return;
			loadError = e instanceof Error ? e.message : m.rpe_load_failed();
		}
	}

	async function save() {
		if (!tenantId || validationErrors.length) return;
		saving = true;
		saveError = null;
		try {
			const def = definition as unknown as Record<string, unknown>;
			if (mode === 'create') await api.responsePlaybooks.createAuthored(tenantId, def);
			else await api.responsePlaybooks.updateAuthored(tenantId, pid, def);
			await localizedGoto('/response-playbooks');
		} catch (e) {
			saveError = e instanceof Error ? e.message : m.rp_save_failed();
		} finally {
			saving = false;
		}
	}

	function capListFor(which: Which) {
		return which === 'close' ? CAPABILITIES.filter((c) => c.onCloseAllowed) : CAPABILITIES;
	}

	interface Section {
		which: Which;
		label: () => string;
		list: ActionState[];
	}
	$: sections = [
		{ which: 'escalate' as Which, label: m.rpe_on_escalate, list: onEscalate },
		{ which: 'close' as Which, label: m.rpe_on_close, list: onClose }
	] satisfies Section[];
</script>

<svelte:head>
	<title>{mode === 'create' ? m.rpe_new_title() : m.rpe_edit_title({ id: editId ?? '' })} - SocTalk</title>
</svelte:head>

<div class="flex items-center justify-between mb-4">
	<h1 class="h2">{mode === 'create' ? m.rpe_new_title() : m.rpe_edit_title({ id: editId ?? '' })}</h1>
	<a class="btn btn-sm variant-soft" href={localizeHref('/response-playbooks')}>{m.common_back()}</a>
</div>

{#if !tenantId}
	<div class="card p-6 opacity-60 text-sm">{m.rpe_pin_hint()}</div>
{:else if !loaded}
	<div class="card p-6 opacity-60 text-sm">{loadError ?? m.common_loading()}</div>
{:else}
	<div class="grid xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-6 items-start">
	<div class="grid gap-6 content-start min-w-0" data-testid="response-editor">
		<!-- Identity -->
		<div class="card p-4 grid gap-3">
			<h3 class="h4">{m.rpe_identity()}</h3>
			<label class="label">
				<span class="text-sm">{m.rpe_id_label()}</span>
				<input
					class="input"
					data-testid="rp-id"
					bind:value={pid}
					disabled={mode === 'edit'}
					placeholder="my-response-playbook"
				/>
			</label>
			<div class="grid grid-cols-2 gap-3">
				<label class="label"
					><span class="text-sm">{m.rpe_version()}</span><input class="input" type="number" bind:value={version} /></label
				>
				<label class="label"
					><span class="text-sm">{m.rpe_priority()}</span><input class="input" type="number" bind:value={priority} /></label
				>
			</div>
			{#if existingStatus}
				<p class="text-xs opacity-60">
					{m.rpe_current_status()}
					<span class="badge variant-soft text-xs">{existingStatus}</span>
					{m.rpe_status_note()}
				</p>
			{/if}
		</div>

		<!-- Applies to -->
		<div class="card p-4 grid gap-3">
			<h3 class="h4">{m.rpe_applies_to()}</h3>
			<p class="text-xs opacity-60">{m.rpe_applies_hint()}</p>
			<label class="label"
				><span class="text-sm">{m.rpe_rule_groups()}</span
				><input class="input" data-testid="rp-groups" bind:value={ruleGroupsText} placeholder="sudo, su" /></label
			>
			<label class="label"
				><span class="text-sm">{m.rpe_rule_ids()}</span
				><input class="input" bind:value={ruleIdsText} placeholder="5710" /></label
			>
			<div class="grid grid-cols-2 gap-3">
				<label class="label"
					><span class="text-sm">{m.rpe_attck_techniques()}</span
					><input
						class="input"
						data-testid="rp-mitre-techniques"
						bind:value={mitreTechniquesText}
						placeholder="T1078, T1021"
					/><span class="text-[10px] opacity-50">{m.rpe_tech_hint()}</span></label
				>
				<label class="label"
					><span class="text-sm">{m.rpe_attck_tactics()}</span
					><input class="input" bind:value={mitreTacticsText} placeholder="Lateral Movement" /><span
						class="text-[10px] opacity-50">{m.rpe_tactic_hint()}</span
					></label
				>
			</div>
		</div>

		<!-- Action lists -->
		{#each sections as section}
			<div class="card p-4 grid gap-3">
				<div class="flex items-center justify-between">
					<h3 class="h4">{section.label()}</h3>
					<button
						class="btn btn-sm variant-soft"
						data-testid="rp-add-{section.which}"
						on:click={() => addAction(section.which)}>{m.rpe_add_action()}</button
					>
				</div>
				{#if section.list.length === 0}
					<p class="text-xs opacity-60">{m.rpe_no_actions()}</p>
				{/if}
				{#each section.list as a, i}
					<div class="card variant-soft p-3 grid gap-2">
						<div class="flex items-center gap-2">
							<select class="select" data-testid="rp-{section.which}-cap-{i}" bind:value={a.capability}>
								{#each capListFor(section.which) as c}
									<option value={c.name}>{capLabel(c.name)}{c.autonomous ? '' : m.rpe_needs_approval()}</option>
								{/each}
							</select>
							<button class="btn btn-sm variant-soft-error" on:click={() => removeAction(section.which, i)}
								>{m.rpe_remove()}</button
							>
						</div>
						{#if CAP_BY_NAME[a.capability] && !CAP_BY_NAME[a.capability].autonomous}
							<p class="text-xs opacity-60">{m.rpe_gated_hint()}</p>
						{/if}
						<label class="label">
							<span class="text-xs opacity-70">{m.rpe_params()}</span>
							<textarea class="textarea font-mono text-xs h-20" bind:value={a.paramsText}></textarea>
						</label>
						{#if a.paramsError}
							<p class="text-xs text-error-500">{a.paramsError}</p>
						{/if}
						<label class="flex items-center gap-2 text-sm">
							<input type="checkbox" class="checkbox" bind:checked={a.useWhen} />
							<span>{m.rpe_only_when()}</span>
						</label>
						{#if a.useWhen}
							<div class="flex items-center gap-2 flex-wrap">
								<select class="select w-auto" bind:value={a.when.field}>
									{#each ALL_FIELDS as f}<option value={f}>{f}</option>{/each}
								</select>
								{#if isListField(a.when.field)}
									<span class="badge variant-soft text-xs">{m.rpe_contains()}</span>
								{:else}
									<select class="select w-auto" bind:value={a.when.op}>
										{#each COMPARISONS as op}<option value={op}>{op}</option>{/each}
									</select>
								{/if}
								<input class="input w-40" bind:value={a.when.value} placeholder={m.rpe_value_placeholder()} />
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/each}

		<!-- Validation + save -->
		{#if validationErrors.length}
			<div class="alert variant-soft-warning text-sm">
				<ul class="list-disc ml-4">
					{#each validationErrors as e}<li>{e}</li>{/each}
				</ul>
			</div>
		{/if}
		{#if saveError}
			<div class="alert variant-filled-error text-sm"><span>{saveError}</span></div>
		{/if}
		<div class="flex items-center gap-3">
			<button
				class="btn variant-filled-primary"
				data-testid="rp-save"
				on:click={save}
				disabled={saving || validationErrors.length > 0}
			>
				{saving ? m.common_saving() : mode === 'create' ? m.rpe_create_shadow() : m.common_save()}
			</button>
			<a class="btn variant-soft" href={localizeHref('/response-playbooks')}>{m.common_cancel()}</a>
			<span class="text-xs opacity-50 ml-auto">{m.rpe_saved_shadow_note()}</span>
		</div>

		<details class="card p-3">
			<summary class="cursor-pointer text-sm opacity-70">{m.rpe_preview_json()}</summary>
			<pre class="text-xs mt-2 overflow-x-auto">{JSON.stringify(definition, null, 2)}</pre>
		</details>
	</div>

	<!-- Live flow diagram: the disposition envelope fans out to the actions;
	     gated actions route through human approval before executing. -->
	<aside class="hidden xl:block">
		<div class="card p-1 sticky top-4 h-[calc(100vh-7rem)] flex flex-col">
			<div class="flex items-center justify-between px-3 pt-2 pb-1">
				<h3 class="h4">{m.rpe_flow()}</h3>
				<span class="text-xs opacity-50">{m.rpe_flow_note()}</span>
			</div>
			<div class="flex-1 min-h-0">
				<ResponseFlowPreview {definition} />
			</div>
		</div>
	</aside>
	</div>
{/if}
