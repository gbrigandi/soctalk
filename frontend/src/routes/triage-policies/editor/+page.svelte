<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { api } from '$lib/api/client';
	import { m } from '$lib/paraglide/messages';
	import { localizeHref, localizedGoto } from '$lib/i18n';
	import { authSession, currentTenantId, isMsspUser } from '$lib/stores';
	import ConditionBuilder from '$lib/triage-policy/ConditionBuilder.svelte';
	import TriagePolicyFlowPreview from '$lib/triage-policy/TriagePolicyFlowPreview.svelte';
	import {
		AUTHORIZATION_TRACKS,
		FILE_PRIORITY_FLOOR,
		GUARDRAIL_TARGETS,
		KNOWN_DECISION_MODULES,
		KNOWN_STEP_NODES,
		MAX_GUARDRAILS,
		STATE_CONTRACT,
		GRANTABLE_ACTIONS,
		conditionToGroup,
		emptyGroup,
		emptyRule,
		groupToCondition,
		simulateGuard,
		validateDefinition,
		type GuardrailDef,
		type TriagePolicyDef,
		type RuleGroup
	} from '$lib/triage-policy/schema';

	// ---------------------------------------------------------------- state

	// MSSP-side editor (same audience wall as /triage-policies): bounce a
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

	let pid = 'my-triage-policy';
	let priority = 70;
	let version = 1;
	let lifecycleStatus: 'draft' | 'shadow' = 'shadow';
	// The stored row's status when editing ('active' included). The PUT route
	// preserves active + queues a rollout; the UI must say so, not imply shadow.
	let existingStatus: string | null = null;

	let ruleGroupsText = '';
	let ruleIdsText = '';
	let tracks: Record<string, boolean> = { account: false, fim: false };

	let stepChecked: Record<string, boolean> = Object.fromEntries(
		KNOWN_STEP_NODES.map((s) => [s, false])
	);
	let moduleChecked: Record<string, boolean> = Object.fromEntries(
		KNOWN_DECISION_MODULES.map((m) => [m, false])
	);

	let constrainTriage = false;
	let constrainDecide = false;
	let triageActions: Record<string, boolean> = Object.fromEntries(
		GRANTABLE_ACTIONS.map((a) => [a, true])
	);
	let decideActions: Record<string, boolean> = Object.fromEntries(
		GRANTABLE_ACTIONS.map((a) => [a, true])
	);

	let signoffText = '';

	interface GuardrailState {
		effect: 'override' | 'interrupt';
		to: 'escalate' | 'needs_more_info' | 'human_review';
		reason: string;
		/** null = the condition isn't representable in the builder → JSON mode. */
		builder: RuleGroup | null;
		rawWhen: string;
		/** Blocking problem (invalid JSON) — disables save. */
		rawError: string | null;
		/** Informational note (e.g. not representable visually) — never blocks. */
		rawNotice: string | null;
	}
	let guardrails: GuardrailState[] = [];

	let showJson = false;
	let jsonText = '';
	let jsonError: string | null = null;
	let flowCompact = false;

	let saving = false;
	let saveError: string | null = null;

	// ------------------------------------------------------------- document

	function csv(text: string): string[] {
		return text
			.split(',')
			.map((s) => s.trim())
			.filter((s) => s.length > 0);
	}

	function guardrailWhen(g: GuardrailState): Record<string, unknown> {
		if (g.builder !== null) {
			return groupToCondition(g.builder) ?? {};
		}
		try {
			return JSON.parse(g.rawWhen);
		} catch {
			return {};
		}
	}

	function buildDefinition(..._deps: unknown[]): TriagePolicyDef {
		const def: TriagePolicyDef = { id: pid, priority: Number(priority) };
		if (version !== 1) def.version = version;
		const applies: TriagePolicyDef['applies_to'] = {};
		if (csv(ruleGroupsText).length) applies.rule_groups = csv(ruleGroupsText);
		if (csv(ruleIdsText).length) applies.rule_ids = csv(ruleIdsText);
		const activeTracks = AUTHORIZATION_TRACKS.filter((t) => tracks[t]);
		if (activeTracks.length) applies.authorization_tracks = activeTracks;
		if (Object.keys(applies).length) def.applies_to = applies;
		const steps = KNOWN_STEP_NODES.filter((s) => stepChecked[s]);
		if (steps.length) def.required_steps = steps;
		const modules = KNOWN_DECISION_MODULES.filter((m) => moduleChecked[m]);
		if (modules.length) def.decision_modules = modules;
		const legal: Record<string, string[]> = {};
		if (constrainTriage) legal.triage = GRANTABLE_ACTIONS.filter((a) => triageActions[a]);
		if (constrainDecide) legal.decide = GRANTABLE_ACTIONS.filter((a) => decideActions[a]);
		if (Object.keys(legal).length) def.legal_actions = legal;
		if (csv(signoffText).length) def.close_signoff_data_classes = csv(signoffText);
		if (guardrails.length) {
			def.guardrails = guardrails.map(
				(g): GuardrailDef => ({
					when: guardrailWhen(g),
					effect: g.effect,
					to: g.to,
					reason: g.reason
				})
			);
		}
		return def;
	}

	$: definition = buildDefinition(
		pid,
		priority,
		version,
		ruleGroupsText,
		ruleIdsText,
		tracks,
		stepChecked,
		moduleChecked,
		constrainTriage,
		constrainDecide,
		triageActions,
		decideActions,
		signoffText,
		guardrails
	);

	$: validationErrors = [
		...guardrails.flatMap((g, i) =>
			g.rawError ? [m.tp_validate_guardrail_condition({ n: i + 1, error: g.rawError })] : []
		),
		...validateDefinition(definition as unknown as Record<string, unknown>)
	];

	function loadDefinition(def: Record<string, unknown>) {
		pid = String(def.id ?? '');
		priority = Number(def.priority ?? 70);
		version = Number(def.version ?? 1);
		const applies = (def.applies_to ?? {}) as Record<string, string[]>;
		ruleGroupsText = (applies.rule_groups ?? []).join(', ');
		ruleIdsText = (applies.rule_ids ?? []).join(', ');
		tracks = Object.fromEntries(
			AUTHORIZATION_TRACKS.map((t) => [t, (applies.authorization_tracks ?? []).includes(t)])
		);
		stepChecked = Object.fromEntries(
			KNOWN_STEP_NODES.map((s) => [s, ((def.required_steps as string[]) ?? []).includes(s)])
		);
		moduleChecked = Object.fromEntries(
			KNOWN_DECISION_MODULES.map((m) => [m, ((def.decision_modules as string[]) ?? []).includes(m)])
		);
		const legal = (def.legal_actions ?? {}) as Record<string, string[]>;
		constrainTriage = Array.isArray(legal.triage);
		constrainDecide = Array.isArray(legal.decide);
		triageActions = Object.fromEntries(
			GRANTABLE_ACTIONS.map((a) => [a, (legal.triage ?? []).includes(a)])
		);
		decideActions = Object.fromEntries(
			GRANTABLE_ACTIONS.map((a) => [a, (legal.decide ?? []).includes(a)])
		);
		signoffText = ((def.close_signoff_data_classes as string[]) ?? []).join(', ');
		guardrails = (((def.guardrails as GuardrailDef[]) ?? []) || []).map((g) => ({
			effect: g.effect,
			to: g.to,
			reason: g.reason,
			builder: conditionToGroup(g.when),
			rawWhen: JSON.stringify(g.when, null, 2),
			rawError: null,
			rawNotice:
				conditionToGroup(g.when) === null
					? m.tp_builder_unrepresentable_json_notice()
					: null
		}));
	}

	onMount(() => {
		if (!editId) loaded = true;
	});

	// Keyed load (Codex High): SvelteKit reuses this component across ?id=
	// navigations and the tenant pin can change mid-session — a one-shot flag
	// left stale form data behind. Reload whenever (tenant, id) changes and
	// discard responses that arrive for a key we've since navigated away from.
	let loadedKey = '';
	$: loadKey =
		editId && tenantId && $authSession.user && $isMsspUser ? `${tenantId}|${editId}` : '';
	$: if (loadKey && loadKey !== loadedKey) {
		loadedKey = loadKey;
		loaded = false;
		loadError = null;
		existingStatus = null;
		loadExisting(tenantId!, editId!, loadKey);
	}
	$: if (!loadKey && loadedKey) {
		loadedKey = ''; // back to create mode in the same component instance
		loaded = true;
	}

	async function loadExisting(tid: string, id: string, key: string) {
		try {
			// listAuthored + find: there is no single-get endpoint yet.
			const all = await api.triagePolicies.listAuthored(tid);
			if (key !== loadedKey) return; // stale response — a newer load owns the form
			const row = all.find((p) => p.triage_policy_id === id);
			if (!row) throw new Error(m.tp_not_found({ id }));
			existingStatus = row.status;
			lifecycleStatus = row.status === 'draft' ? 'draft' : 'shadow';
			loadDefinition(row.definition);
			loaded = true;
		} catch (e) {
			if (key !== loadedKey) return;
			loadError = e instanceof Error ? e.message : m.tp_load_one_failed();
		}
	}

	// ------------------------------------------------------------ guardrails

	function addGuardrail() {
		const g = emptyGroup();
		g.children = [emptyRule()];
		guardrails = [
			...guardrails,
			{
				effect: 'override',
				to: 'escalate',
				reason: '',
				builder: g,
				rawWhen: '',
				rawError: null,
				rawNotice: null
			}
		];
	}

	function removeGuardrail(i: number) {
		guardrails = guardrails.filter((_, idx) => idx !== i);
	}

	function moveGuardrail(i: number, delta: number) {
		const j = i + delta;
		if (j < 0 || j >= guardrails.length) return;
		const next = [...guardrails];
		[next[i], next[j]] = [next[j], next[i]];
		guardrails = next;
	}

	function onEffectChange(g: GuardrailState) {
		// keep the pair legal: interrupt → human_review; override → a disposition
		if (g.effect === 'interrupt') g.to = 'human_review';
		else if (g.to === 'human_review') g.to = 'escalate';
		guardrails = guardrails;
	}

	function toJsonMode(g: GuardrailState) {
		g.rawWhen = JSON.stringify(g.builder ? (groupToCondition(g.builder) ?? {}) : {}, null, 2);
		g.builder = null;
		guardrails = guardrails;
	}

	function toBuilderMode(g: GuardrailState) {
		try {
			const parsed = JSON.parse(g.rawWhen);
			const group = conditionToGroup(parsed);
			if (group === null) {
				g.rawNotice = m.tp_builder_unrepresentable_keep_json_notice();
				guardrails = guardrails;
				return;
			}
			g.builder = group;
			g.rawError = null;
			g.rawNotice = null;
		} catch {
			g.rawError = m.tp_invalid_json_lower();
		}
		guardrails = guardrails;
	}

	function onRawWhenChange(g: GuardrailState, text: string) {
		g.rawWhen = text;
		try {
			JSON.parse(text);
			g.rawError = null;
		} catch {
			g.rawError = m.tp_invalid_json_lower();
		}
		guardrails = guardrails;
	}

	// ---------------------------------------------------------- JSON document

	function openJson() {
		jsonText = JSON.stringify(definition, null, 2);
		jsonError = null;
		showJson = true;
	}

	function applyJson() {
		try {
			const parsed = JSON.parse(jsonText);
			loadDefinition(parsed);
			jsonError = null;
			showJson = false;
		} catch (e) {
			jsonError = e instanceof Error ? e.message : m.tp_invalid_json_no_period();
		}
	}

	// ------------------------------------------------------------- test panel

	let sim = {
		verdict: 'close',
		confidence: 0.8,
		authzClass: 'absent',
		ioc: false,
		activeIncident: false,
		dataClass: '',
		environment: '',
		criticality: ''
	};

	$: simCtx = {
		authz: {
			class: sim.authzClass,
			in_scope: sim.authzClass === 'covered',
			sanctioned_or_routine: sim.authzClass === 'covered',
			actor_genuine: true,
			policy_allowed: sim.authzClass !== 'contradicted'
		},
		verdict: sim.verdict,
		verdict_confidence: sim.confidence,
		asset: {
			data_classification: sim.dataClass || null,
			environment: sim.environment || null,
			criticality: sim.criticality || null
		},
		enrichment: { ioc: sim.ioc },
		correlation: { active_incident: sim.activeIncident }
	};

	$: simResult = simulateGuard(definition, simCtx);

	$: firedNodeId =
		simResult.stage === 'guardrail'
			? `guardrail-${simResult.index}`
			: simResult.stage; // 'floor' | 'signoff' | 'commit' match the node ids

	const SIM_BADGE: Record<string, string> = {
		close: 'variant-filled-success',
		needs_more_info: 'variant-filled-warning',
		escalate: 'variant-filled-error',
		human_review: 'variant-filled-tertiary'
	};

	// ------------------------------------------------------------------- save

	async function save() {
		if (!tenantId) return;
		saving = true;
		saveError = null;
		try {
			const doc = definition as unknown as Record<string, unknown>;
			if (mode === 'create') await api.triagePolicies.createAuthored(tenantId, doc, lifecycleStatus);
			else await api.triagePolicies.updateAuthored(tenantId, editId!, doc, lifecycleStatus);
			await localizedGoto('/triage-policies');
		} catch (e) {
			saveError = e instanceof Error ? e.message : m.tp_save_failed();
		} finally {
			saving = false;
		}
	}

	function focusGuardrail(e: CustomEvent<{ guardrail: number }>) {
		document
			.getElementById(`guardrail-card-${e.detail.guardrail}`)
			?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}
</script>

<svelte:head>
	<title>{mode === 'edit' ? m.tp_editor_head_edit({ id: editId ?? '' }) : m.tp_new_policy_title()} - SocTalk</title>
</svelte:head>

<div class="flex items-center justify-between mb-1">
	<h1 class="h2">{mode === 'edit' ? m.tp_edit_policy_title() : m.tp_new_policy_title()}</h1>
	<div class="flex gap-2">
		<button class="btn btn-sm variant-soft" on:click={openJson}>{m.tp_view_as_json()}</button>
		<a class="btn btn-sm variant-soft" href={localizeHref('/triage-policies')}>{m.tp_cancel()}</a>
		<button
			class="btn btn-sm variant-filled-primary"
			on:click={save}
			disabled={saving || !tenantId || validationErrors.length > 0}
		>
			{saving ? m.tp_saving() : mode === 'create' ? m.tp_create_shadow() : m.tp_save_revision()}
		</button>
	</div>
</div>
<p class="opacity-60 text-sm mb-4">
	{m.tp_editor_intro()}
</p>

{#if !tenantId}
	<div class="card p-6 opacity-60 text-sm">{m.tp_editor_pin_hint()}</div>
{:else if loadError}
	<div class="alert variant-filled-error"><span>{loadError}</span></div>
{:else if !loaded}
	<div class="flex items-center justify-center h-40">
		<div class="animate-spin rounded-full h-10 w-10 border-b-2 border-primary-500"></div>
	</div>
{:else}
	<div class="grid grid-cols-1 xl:grid-cols-5 gap-4 items-start">
		<!-- ------------------------------------------------ form column -->
		<div class="xl:col-span-3 space-y-4">
			<section class="card p-4 space-y-3">
				<h3 class="h4">{m.tp_identity()}</h3>
				<div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
					<label class="label text-sm sm:col-span-2">
						<span class="opacity-70">{m.tp_policy_id_label()}</span>
						<input
							class="input font-mono"
							bind:value={pid}
							disabled={mode === 'edit'}
							placeholder="my-triage-policy"
						/>
					</label>
					<label class="label text-sm">
						<span class="opacity-70"
							>{m.tp_priority_label({ floor: FILE_PRIORITY_FLOOR })}</span
						>
						<input class="input" type="number" min={FILE_PRIORITY_FLOOR} bind:value={priority} />
					</label>
				</div>
				{#if existingStatus === 'active'}
					<div class="flex items-center gap-2 text-sm">
						<span class="badge variant-filled-success text-xs">active</span>
						<span class="opacity-70">
							{m.tp_active_revision_note()}
						</span>
					</div>
				{:else}
					<label class="flex items-center gap-2 text-sm">
						<input class="checkbox" type="checkbox" checked={lifecycleStatus === 'draft'}
							on:change={(e) => (lifecycleStatus = e.currentTarget.checked ? 'draft' : 'shadow')} />
						<span>{m.tp_keep_as_draft()}</span>
					</label>
				{/if}
			</section>

			<section class="card p-4 space-y-3">
				<h3 class="h4">{m.tp_match_section_title()}</h3>
				<p class="text-xs opacity-60">
					{m.tp_match_section_hint()}
				</p>
				<label class="label text-sm">
					<span class="opacity-70">{m.tp_wazuh_rule_groups()}</span>
					<input class="input" bind:value={ruleGroupsText} placeholder="sudo, su" />
				</label>
				<label class="label text-sm">
					<span class="opacity-70">{m.tp_rule_ids()}</span>
					<input class="input" bind:value={ruleIdsText} placeholder="5402, 5501" />
				</label>
				<div class="text-sm">
					<span class="opacity-70">{m.tp_authorization_activity_tracks()}</span>
					<div class="flex gap-4 mt-1">
						{#each AUTHORIZATION_TRACKS as t}
							<label class="flex items-center gap-2">
								<input class="checkbox" type="checkbox" bind:checked={tracks[t]} />
								<span class="font-mono text-xs">{t}</span>
							</label>
						{/each}
					</div>
				</div>
			</section>

			<section class="card p-4 space-y-3">
				<h3 class="h4">{m.tp_investigation_requirements()}</h3>
				<div class="text-sm">
					<span class="opacity-70">{m.tp_required_steps_label()}</span>
					{#each KNOWN_STEP_NODES as s}
						<label class="flex items-center gap-2 mt-1">
							<input class="checkbox" type="checkbox" bind:checked={stepChecked[s]} />
							<span class="font-mono text-xs">{s}</span>
						</label>
					{/each}
				</div>
				<div class="text-sm">
					<span class="opacity-70">{m.tp_decision_modules_to_consult()}</span>
					{#each KNOWN_DECISION_MODULES as mod}
						<label class="flex items-center gap-2 mt-1">
							<input class="checkbox" type="checkbox" bind:checked={moduleChecked[mod]} />
							<span class="font-mono text-xs">{mod}</span>
						</label>
					{/each}
				</div>
				<div class="text-sm space-y-2">
					<span class="opacity-70">{m.tp_allowed_supervisor_actions()}</span>
					{#each ['triage', 'decide'] as phase}
						<div class="flex flex-wrap items-center gap-3">
							<label class="flex items-center gap-2 w-20">
								<input
									class="checkbox"
									type="checkbox"
									checked={phase === 'triage' ? constrainTriage : constrainDecide}
									on:change={(e) => {
										if (phase === 'triage') constrainTriage = e.currentTarget.checked;
										else constrainDecide = e.currentTarget.checked;
									}}
								/>
								<span class="font-mono text-xs">{phase}</span>
							</label>
							{#if phase === 'triage' ? constrainTriage : constrainDecide}
								{#each GRANTABLE_ACTIONS as a}
									<label class="flex items-center gap-1">
										<input
											class="checkbox"
											type="checkbox"
											checked={phase === 'triage' ? triageActions[a] : decideActions[a]}
											on:change={(e) => {
												if (phase === 'triage') {
													triageActions[a] = e.currentTarget.checked;
													triageActions = triageActions;
												} else {
													decideActions[a] = e.currentTarget.checked;
													decideActions = decideActions;
												}
											}}
										/>
										<span class="font-mono text-[10px]">{a}</span>
									</label>
								{/each}
							{/if}
						</div>
					{/each}
				</div>
			</section>

			<section class="card p-4 space-y-3">
				<h3 class="h4">{m.tp_close_signoff()}</h3>
				<label class="label text-sm">
					<span class="opacity-70">
						{m.tp_close_signoff_label()}
					</span>
					<input class="input" bind:value={signoffText} placeholder="pci, phi" />
				</label>
			</section>

			<section class="card p-4 space-y-3">
				<div class="flex items-center justify-between">
					<h3 class="h4">{m.tp_guardrails_title()}</h3>
					<button
						class="btn btn-sm variant-soft"
						on:click={addGuardrail}
						disabled={guardrails.length >= MAX_GUARDRAILS}
					>
						{m.tp_add_guardrail()}
					</button>
				</div>
				<p class="text-xs opacity-60">
					{m.tp_guardrails_hint()}
				</p>

				{#each guardrails as g, i}
					<div class="card variant-soft p-3 space-y-2" id="guardrail-card-{i}">
						<div class="flex items-center gap-2">
							<span class="badge variant-filled text-xs">{i + 1}</span>
							<select class="select w-auto !py-1 text-xs" bind:value={g.effect} on:change={() => onEffectChange(g)}>
								<option value="override">{m.tp_guardrail_effect_override_option()}</option>
								<option value="interrupt">{m.tp_guardrail_effect_interrupt_option()}</option>
							</select>
							{#if g.effect === 'override'}
								<span class="text-xs opacity-60">{m.tp_raise_to()}</span>
								<select class="select w-auto !py-1 text-xs" bind:value={g.to}>
									{#each GUARDRAIL_TARGETS.filter((t) => t !== 'human_review') as t}
										<option value={t}>{t}</option>
									{/each}
								</select>
							{:else}
								<span class="badge variant-soft-tertiary text-xs">→ human_review</span>
							{/if}
							<div class="flex-1"></div>
							<button class="btn-icon btn-icon-sm variant-soft" title={m.tp_move_up()} on:click={() => moveGuardrail(i, -1)} disabled={i === 0}>↑</button>
							<button class="btn-icon btn-icon-sm variant-soft" title={m.tp_move_down()} on:click={() => moveGuardrail(i, 1)} disabled={i === guardrails.length - 1}>↓</button>
							<button class="btn-icon btn-icon-sm variant-soft-error" title={m.tp_remove()} on:click={() => removeGuardrail(i)}>✕</button>
						</div>

						<div class="pl-1">
							{#if g.builder !== null}
								<ConditionBuilder bind:group={g.builder} on:change={() => (guardrails = guardrails)} />
								<button class="anchor text-xs mt-1" on:click={() => toJsonMode(g)}>
									{m.tp_edit_condition_as_json()}
								</button>
							{:else}
								<textarea
									class="textarea font-mono text-xs h-28"
									value={g.rawWhen}
									on:input={(e) => onRawWhenChange(g, e.currentTarget.value)}
								></textarea>
								{#if g.rawError}
									<div class="text-xs text-error-500">{g.rawError}</div>
								{/if}
								{#if g.rawNotice}
									<div class="text-xs text-warning-500">{g.rawNotice}</div>
								{/if}
								<button class="anchor text-xs mt-1" on:click={() => toBuilderMode(g)}>
									{m.tp_back_to_visual_builder()}
								</button>
							{/if}
						</div>

						<label class="label text-sm">
							<span class="opacity-70 text-xs">{m.tp_guardrail_reason_label()}</span>
							<input class="input !py-1 text-sm" bind:value={g.reason}
								placeholder={m.tp_guardrail_reason_placeholder()} maxlength="512" />
						</label>
					</div>
				{/each}
				{#if guardrails.length === 0}
					<div class="text-sm opacity-50 text-center py-2">{m.tp_no_guardrails_yet()}</div>
				{/if}
			</section>

			{#if validationErrors.length}
				<div class="alert variant-soft-error text-sm">
					<div>
						<p class="font-semibold mb-1">{m.tp_fix_before_saving()}</p>
						<ul class="list-disc ml-5 space-y-0.5">
							{#each validationErrors as e}
								<li>{e}</li>
							{/each}
						</ul>
					</div>
				</div>
			{/if}
			{#if saveError}
				<div class="alert variant-filled-error text-sm"><span>{saveError}</span></div>
			{/if}
		</div>

		<!-- --------------------------------------------- preview column -->
		<div
			class="xl:col-span-2 space-y-4 xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)] xl:overflow-y-auto"
		>
			<section class="card p-2">
				<div class="flex items-center justify-between px-2 pt-1">
					<h3 class="h4">{m.tp_decision_flow()}</h3>
					<label class="flex items-center gap-2 text-xs opacity-70">
						<input class="checkbox checkbox-sm" type="checkbox" bind:checked={flowCompact} />
						{m.tp_compact()}
					</label>
				</div>
				<p class="text-xs opacity-60 px-2 pb-1">
					{m.tp_decision_flow_hint()}
				</p>
				<div class="h-[32rem]">
					<TriagePolicyFlowPreview
						{definition}
						{firedNodeId}
						compact={flowCompact}
						on:focus={focusGuardrail}
					/>
				</div>
			</section>

			<section class="card p-4 space-y-2">
				<h3 class="h4">{m.tp_try_it()}</h3>
				<p class="text-xs opacity-60">
					{m.tp_try_it_hint()}
				</p>
				<div class="grid grid-cols-2 gap-2 text-sm">
					<label class="label">
						<span class="opacity-70 text-xs">{m.tp_try_llm_draft_verdict()}</span>
						<select class="select !py-1" bind:value={sim.verdict}>
							<option value="close">close</option>
							<option value="needs_more_info">needs_more_info</option>
							<option value="escalate">escalate</option>
						</select>
					</label>
					<label class="label">
						<span class="opacity-70 text-xs">{m.tp_try_confidence()}</span>
						<input class="input !py-1" type="number" min="0" max="1" step="0.05" bind:value={sim.confidence} />
					</label>
					<label class="label">
						<span class="opacity-70 text-xs">{m.tp_try_authorization_class()}</span>
						<select class="select !py-1" bind:value={sim.authzClass}>
							<option value="covered">covered</option>
							<option value="contradicted">contradicted</option>
							<option value="absent">absent</option>
						</select>
					</label>
					<label class="label">
						<span class="opacity-70 text-xs">{m.tp_try_asset_data_classification()}</span>
						<input class="input !py-1" bind:value={sim.dataClass} placeholder="pci" />
					</label>
					<label class="label">
						<span class="opacity-70 text-xs">{m.tp_try_asset_criticality()}</span>
						<select class="select !py-1" bind:value={sim.criticality}>
							<option value="">{m.tp_unknown_lower()}</option>
							<option value="critical">critical</option>
							<option value="high">high</option>
							<option value="medium">medium</option>
							<option value="low">low</option>
						</select>
					</label>
					<label class="label">
						<span class="opacity-70 text-xs">{m.tp_try_asset_environment()}</span>
						<input class="input !py-1" bind:value={sim.environment} placeholder="production" />
					</label>
					<label class="flex items-center gap-2">
						<input class="checkbox" type="checkbox" bind:checked={sim.ioc} />
						<span class="text-xs">{m.tp_try_malicious_indicator()}</span>
					</label>
					<label class="flex items-center gap-2">
						<input class="checkbox" type="checkbox" bind:checked={sim.activeIncident} />
						<span class="text-xs">{m.tp_try_active_incident()}</span>
					</label>
				</div>
				<div class="card variant-soft p-3 text-sm space-y-1">
					<div class="flex items-center gap-2 flex-wrap">
						<span class="opacity-60 text-xs">{m.tp_try_outcome()}</span>
						<span class="badge {SIM_BADGE[simResult.finalDecision] ?? 'variant-soft'} text-xs">
							{simResult.finalDecision}
						</span>
						{#if simResult.heldForReview}
							<span class="badge variant-filled-tertiary text-xs">
								{m.tp_try_draft_held()}
							</span>
						{/if}
						{#if simResult.stage === 'guardrail'}
							<span class="text-xs opacity-70">
								{m.tp_try_guardrail_fired({
									n: (simResult.index ?? 0) + 1,
									effect: simResult.effect ?? ''
								})}
							</span>
						{:else if simResult.stage === 'floor'}
							<span class="text-xs opacity-70">{m.tp_try_safety_floor()}</span>
						{:else if simResult.stage === 'signoff'}
							<span class="text-xs opacity-70">{m.tp_try_close_signoff_interrupt()}</span>
						{/if}
					</div>
					<p class="text-xs opacity-70">{simResult.reason}</p>
				</div>
				<p class="text-[10px] opacity-40">
					{m.tp_contract_fields_note({ count: STATE_CONTRACT.length })}
				</p>
			</section>
		</div>
	</div>
{/if}

{#if showJson}
	<div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
		<div class="card p-6 max-w-2xl w-full space-y-3">
			<h3 class="h4">{m.tp_json_document_title()}</h3>
			<p class="text-xs opacity-60">
				{m.tp_json_document_hint()}
			</p>
			<textarea class="textarea font-mono text-xs h-80" bind:value={jsonText}></textarea>
			{#if jsonError}
				<div class="alert variant-filled-error text-sm"><span>{jsonError}</span></div>
			{/if}
			<div class="flex justify-end gap-2">
				<button class="btn variant-soft" on:click={() => (showJson = false)}>{m.tp_close()}</button>
				<button class="btn variant-filled-primary" on:click={applyJson}>{m.tp_apply_to_form()}</button>
			</div>
		</div>
	</div>
{/if}
