<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, type Investigation, type InvestigationTimelineEvent } from '$lib/api/client';
	import { addToast, isCustomerScope } from '$lib/stores';
	import { formatStatus, formatPhase, formatSeverity, formatDecision, formatDuration, formatEventType } from '$lib/utils/formatters';
	import { m } from '$lib/paraglide/messages';
	import { localizeHref } from '$lib/i18n';
	import ChatPanel from '$lib/components/chat/ChatPanel.svelte';
	import ReplayPanel from '$lib/pipeline-viz/ReplayPanel.svelte';
	import SegmentedTabs from '$lib/components/SegmentedTabs.svelte';

	let chatOpen = false;

	let investigation: Investigation | null = null;
	let events: InvestigationTimelineEvent[] = [];
	// Flight recorder (#72): the right column toggles Timeline | Replay.
	let eventView: 'timeline' | 'replay' =
		$page.url.searchParams.get('view') === 'replay' ? 'replay' : 'timeline';
	let loading = true;
	let eventsLoading = true;
	let error: string | null = null;
	let actionLoading = false;
	let showCancelModal = false;
	// Budget unlock (#127). A run that hit its per-run ceiling ends
	// `halted_budget`, which also blocks the investigation from ever getting
	// another run, so this is the only way back without database access.
	let unlockOpen = false;
	let unlockDollars = '';
	let unlockBusy = false;
	let unlockError: string | null = null;

	function money(v: number): string {
		if (!v) return '$0.00';
		return Math.abs(v) < 0.01 ? `$${v.toFixed(6)}` : `$${v.toFixed(2)}`;
	}

	async function unlockRun() {
		// Say why rather than doing nothing. A control that silently no-ops
		// looks broken and gives the operator nothing to act on.
		if (!investigation?.run_id) {
			unlockError = 'No run is attached to this investigation.';
			return;
		}
		if (!investigation.tenant_id) {
			unlockError = 'This view did not report a tenant, so the run cannot be addressed.';
			return;
		}
		const n = Number(unlockDollars.trim());
		if (!Number.isFinite(n) || n <= 0) {
			unlockError = 'Enter a new ceiling greater than zero.';
			return;
		}
		// Spend is not reset on resume, so a ceiling at or below what was
		// already spent re-halts immediately. Say so here rather than making
		// the operator discover it from a 422.
		const spent = investigation.dollars_used ?? 0;
		if (n <= spent) {
			unlockError = `Must exceed the ${money(spent)} already spent.`;
			return;
		}
		unlockBusy = true;
		unlockError = null;
		try {
			const res = await api.runBudget.unlockRun(
				investigation.tenant_id,
				investigation.run_id,
				{ dollar_budget: n }
			);
			addToast({
				type: res.warning ? 'warning' : 'success',
				message: res.warning ?? 'Run unlocked; it will be picked up on the next claim.'
			});
			unlockOpen = false;
			await loadInvestigation();
		} catch (e) {
			unlockError = e instanceof Error ? e.message : String(e);
		} finally {
			unlockBusy = false;
		}
	}

	let cancelReason = '';
	let expandedEvents: Set<string> = new Set();

	function toggleEventDetails(eventId: string) {
		if (expandedEvents.has(eventId)) {
			expandedEvents.delete(eventId);
		} else {
			expandedEvents.add(eventId);
		}
		expandedEvents = new Set(expandedEvents);
	}

	function formatEventSummary(eventType: string, data: Record<string, unknown>): string {
		switch (eventType) {
			case 'investigation.created':
				return m.inv_ev_created({ title: String(data.title || m.inv_ev_untitled()) });
			case 'investigation.started':
				return m.inv_ev_started({ phase: formatPhase(data.phase as string) || formatPhase('triage') });
			case 'investigation.closed':
				return m.inv_ev_closed();
			case 'investigation.paused':
				return m.inv_ev_paused();
			case 'investigation.resumed':
				return m.inv_ev_resumed();
			case 'investigation.cancelled':
				return data.reason
					? m.inv_ev_cancelled_reason({ reason: String(data.reason) })
					: m.inv_ev_cancelled();
			case 'investigation.escalated':
				return m.inv_ev_escalated();
			case 'investigation.auto_closed':
				return m.inv_ev_auto_closed();
			case 'alert.added':
				return m.inv_ev_alert_added({ id: String(data.alert_id || m.common_unknown()) });
			case 'alert.correlated':
				return m.inv_ev_alert_correlated({ desc: String(data.description || data.alert_id || m.inv_ev_unknown_alert()) });
			case 'observable.extracted':
				return m.inv_ev_observable({ type: String(data.observable_type), value: String(data.observable_value) });
			case 'enrichment.requested':
				return m.inv_ev_enrichment_requested({ source: String(data.enrichment_type || m.inv_ev_external_source()) });
			case 'enrichment.completed': {
				const result = data.result as Record<string, number> | undefined;
				if (result && typeof result.malicious === 'number') {
					return m.inv_ev_enrichment_result({
						type: String(data.enrichment_type),
						malicious: result.malicious,
						suspicious: result.suspicious || 0,
						value: String(data.observable_value)
					});
				}
				return m.inv_ev_enrichment_completed({ value: String(data.observable_value) });
			}
			case 'enrichment.failed':
				return m.inv_ev_enrichment_failed({ value: String(data.observable_value), error: String(data.error || m.inv_unknown_error()) });
			case 'phase.changed':
				return m.inv_ev_phase_changed({
					from: formatPhase(data.old_phase as string) || '?',
					to: formatPhase(data.new_phase as string || data.phase as string) || '?'
				});
			case 'verdict.rendered':
			case 'verdict.proposed':
				return m.inv_ev_verdict({ decision: formatDecision(data.decision as string), pct: Math.round((data.confidence as number || 0) * 100) });
			case 'human.review_requested':
			case 'review.requested':
				return m.inv_ev_review_requested({ reason: String(data.reason || m.inv_ev_manual_review()) });
			case 'human.decision_received':
				return m.inv_ev_human_decision({ decision: formatDecision(data.decision as string) });
			case 'thehive.case_created':
				return m.inv_ev_thehive_case({ id: String(data.case_id || m.common_unknown()) });
			case 'thehive.alert_promoted':
				return m.inv_ev_alert_promoted();
			case 'misp.ioc_matched':
				return m.inv_ev_misp_ioc();
			case 'misp.context_retrieved':
				return m.inv_ev_misp_context();
			case 'analyzer.invoked':
				return m.inv_ev_analyzer_invoked({ name: String(data.analyzer || m.common_unknown()) });
			case 'analyzer.completed':
				return m.inv_ev_analyzer_completed({ name: String(data.analyzer || m.common_unknown()) });
			case 'error.occurred':
				return m.inv_error({ error: String(data.message || data.error || m.inv_unknown_error()) });
			default:
				return formatEventType(eventType);
		}
	}

	function getEventDetails(eventType: string, data: Record<string, unknown>): Array<{label: string, value: string, highlight?: boolean}> {
		const details: Array<{label: string, value: string, highlight?: boolean}> = [];

		switch (eventType) {
			case 'investigation.created':
				if (data.alert_ids) details.push({ label: m.inv_col_alerts(), value: m.inv_alerts_count({ count: (data.alert_ids as string[]).length }) });
				if (data.source_ip) details.push({ label: m.inv_detail_source_ip(), value: String(data.source_ip) });
				if (data.source_agent) details.push({ label: m.inv_detail_agent(), value: String(data.source_agent) });
				if (data.max_severity) details.push({ label: m.inv_col_severity(), value: String(data.max_severity).toUpperCase(), highlight: true });
				break;
			case 'alert.correlated':
				if (data.rule_id) details.push({ label: m.inv_detail_rule_id(), value: String(data.rule_id) });
				if (data.severity) details.push({ label: m.inv_col_severity(), value: String(data.severity).toUpperCase(), highlight: true });
				if (data.description) details.push({ label: m.inv_detail_description(), value: String(data.description) });
				break;
			case 'observable.extracted':
				if (data.classification) details.push({ label: m.inv_detail_classification(), value: String(data.classification) });
				break;
			case 'enrichment.completed': {
				const result = data.result as Record<string, number> | undefined;
				if (result) {
					if (typeof result.malicious === 'number') details.push({ label: m.inv_col_malicious(), value: String(result.malicious), highlight: result.malicious > 0 });
					if (typeof result.suspicious === 'number') details.push({ label: m.inv_suspicious(), value: String(result.suspicious) });
					if (typeof result.harmless === 'number') details.push({ label: m.inv_detail_harmless(), value: String(result.harmless) });
				}
				break;
			}
			case 'verdict.rendered':
			case 'verdict.proposed':
				if (data.assessment) details.push({ label: m.inv_detail_assessment(), value: String(data.assessment) });
				if (data.recommendation) details.push({ label: m.inv_detail_recommendation(), value: String(data.recommendation) });
				if (data.evidence) {
					const evidence = data.evidence as string[];
					evidence.forEach((e, i) => details.push({ label: i === 0 ? m.inv_detail_evidence() : '', value: e }));
				}
				break;
		}

		return details;
	}

	$: investigationId = $page.params.id as string;

	onMount(async () => {
		if (!investigationId) return;
		await loadInvestigation();
		await loadEvents();
	});

	async function refreshInvestigation() {
		if (!investigationId) return;
		investigation = await api.investigations.get(investigationId);
	}

	async function loadInvestigation() {
		if (!investigationId) return;
		loading = true;
		error = null;
		try {
			await refreshInvestigation();
		} catch (e) {
			error = e instanceof Error ? e.message : m.inv_load_one_failed();
		} finally {
			loading = false;
		}
	}

	async function loadEvents() {
		if (!investigationId) return;
		eventsLoading = true;
		try {
			events = await api.investigations.getEvents(investigationId, 100);
		} catch (e) {
			console.error('Failed to load events:', e);
		} finally {
			eventsLoading = false;
		}
	}

	async function handleCancel() {
		if (!investigationId) return;
		actionLoading = true;
		try {
			const result = await api.investigations.cancel(investigationId, cancelReason);
			await Promise.all([refreshInvestigation(), loadEvents()]);
			addToast({ type: 'success', message: result.message });
			showCancelModal = false;
			cancelReason = '';
		} catch (e) {
			addToast({ type: 'error', message: e instanceof Error ? e.message : m.inv_cancel_failed() });
		} finally {
			actionLoading = false;
		}
	}

	function getStatusBadge(status: string): string {
		switch (status) {
			case 'pending': return 'variant-soft-warning';
			case 'in_progress': return 'variant-soft-primary';
			case 'paused': return 'variant-soft-tertiary';
			case 'closed':
			case 'auto_closed':
				return 'variant-soft-success';
			case 'escalated':
			case 'rejected':
				return 'variant-soft-error';
			case 'cancelled':
				return 'variant-soft';
			default: return 'variant-soft';
		}
	}

	function getSeverityBadge(severity: string | null): string {
		switch (severity?.toLowerCase()) {
			case 'critical': return 'variant-filled-error';
			case 'high': return 'variant-filled-warning';
			case 'medium': return 'variant-filled-secondary';
			case 'low': return 'variant-filled-tertiary';
			default: return 'variant-soft';
		}
	}

	function getVerdictBadge(verdict: string | null): string {
		switch (verdict) {
			case 'escalate': return 'variant-filled-error';
			case 'needs_more_info':
			case 'suspicious':
				return 'variant-filled-warning';
			case 'close':
			case 'auto_close':
				return 'variant-filled-success';
			default: return 'variant-soft';
		}
	}

	function getEventIcon(eventType: string): string {
		switch (eventType) {
			case 'investigation.created':
				return 'M12 4v16m8-8H4';
			case 'investigation.closed':
				return 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z';
			case 'alert.added':
				return 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z';
			case 'observable.extracted':
				return 'M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z';
			case 'enrichment.requested':
			case 'enrichment.completed':
			case 'enrichment.failed':
				return 'M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z';
			case 'verdict.rendered':
				return 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z';
			case 'human.review_requested':
			case 'human.decision_received':
				return 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z';
			case 'thehive.case_created':
				return 'M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z';
			case 'phase.changed':
				return 'M13 9l3 3m0 0l-3 3m3-3H8m13 0a9 9 0 11-18 0 9 9 0 0118 0z';
			default:
				return 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z';
		}
	}

	function getEventColor(eventType: string): string {
		if (eventType.includes('error') || eventType.includes('failed')) return 'text-error-500';
		if (eventType.includes('verdict') || eventType.includes('closed')) return 'text-success-500';
		if (eventType.includes('review') || eventType.includes('human')) return 'text-warning-500';
		if (eventType.includes('enrichment')) return 'text-secondary-500';
		return 'text-primary-500';
	}
</script>

<svelte:head>
	<title>{investigation?.title || m.ttl_investigation()} - SocTalk</title>
</svelte:head>

{#if !loading && investigation}
	<!-- Floating "Ask AI" trigger; opens the chat dock as an overlay. -->
	<button
		type="button"
		class="chat-launcher btn variant-filled-primary"
		on:click={() => (chatOpen = !chatOpen)}
		title={m.inv_ask_ai_title()}
	>
		<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
		</svg>
		{chatOpen ? m.inv_close_chat() : m.inv_ask_ai()}
	</button>
	{#if chatOpen}
		<aside class="chat-dock">
			<ChatPanel investigationId={investigation.id} />
		</aside>
	{/if}
{/if}

{#if loading}
	<div class="flex items-center justify-center h-64">
		<div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500"></div>
	</div>
{:else if error}
	<div class="alert variant-filled-error">
		<span>{m.inv_error({ error })}</span>
	</div>
{:else if investigation}
	<!-- Header -->
	<div class="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4 mb-6">
		<div>
			<div class="flex items-center gap-2 mb-2">
				<a href={localizeHref('/investigations')} class="btn btn-sm variant-soft">
					<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
					</svg>
					{m.inv_back()}
				</a>
			</div>
			<h1 class="h2">{investigation.title || m.inv_untitled()}</h1>
			<div class="flex flex-wrap items-center gap-2 mt-2">
				<span class="badge {getStatusBadge(investigation.status)}">{formatStatus(investigation.status)}</span>
				<span class="badge variant-soft">{formatPhase(investigation.phase)}</span>
				{#if investigation.max_severity}
					<span class="badge {getSeverityBadge(investigation.max_severity)}">{formatSeverity(investigation.max_severity)}</span>
				{/if}
				{#if investigation.verdict_decision}
					<span class="badge {getVerdictBadge(investigation.verdict_decision)}">{formatDecision(investigation.verdict_decision)}</span>
				{/if}
				{#each investigation.tags as tag}
					<span class="badge variant-ghost">{tag}</span>
				{/each}
			</div>
		</div>

		<!-- Action Buttons -->
		<!-- Cancel is the only lifecycle action wired to the backend. Pause/resume
		     were removed (issue #16): the runs worker has no pause semantics, so
		     those buttons would 404 or lie about backend state. Cancel is shown
		     for any non-terminal investigation. -->
		<div class="flex gap-2">
			{#if !['closed', 'auto_closed_fp', 'closed_fp', 'closed_tp', 'cancelled'].includes(investigation.status)}
				<button
					class="btn variant-soft-error"
					disabled={actionLoading}
					on:click={() => showCancelModal = true}
				>
					<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
					{m.common_cancel()}
				</button>
			{/if}
		</div>
	</div>

	<!-- Info Cards -->
	<div class="grid grid-cols-2 lg:grid-cols-6 gap-4 mb-6">
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.inv_col_alerts()}</h4>
			<p class="text-2xl font-bold">{investigation.alert_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.inv_observables()}</h4>
			<p class="text-2xl font-bold">{investigation.observable_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60 text-error-500">{m.inv_col_malicious()}</h4>
			<p class="text-2xl font-bold text-error-500">{investigation.malicious_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60 text-warning-500">{m.inv_suspicious()}</h4>
			<p class="text-2xl font-bold text-warning-500">{investigation.suspicious_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.inv_time_to_triage()}</h4>
			<p class="text-2xl font-bold">{formatDuration(investigation.time_to_triage_seconds)}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.inv_time_to_verdict()}</h4>
			<p class="text-2xl font-bold">{formatDuration(investigation.time_to_verdict_seconds)}</p>
		</div>
	</div>

	<!-- Main Content Grid -->
	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
		<!-- Left Column: Details + Verdict -->
		<div class="space-y-6">
			<!-- Investigation Details -->
			<div class="card p-4">
				<h3 class="h4 mb-4">{m.inv_details()}</h3>
				<dl class="space-y-2">
					<div class="flex justify-between">
						<dt class="opacity-60">{m.inv_label_id()}</dt>
						<dd class="font-mono text-xs">{investigation.id.slice(0, 8)}...</dd>
					</div>
					<div class="flex justify-between">
						<dt class="opacity-60">{m.inv_col_created()}</dt>
						<dd>{new Date(investigation.created_at).toLocaleString()}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="opacity-60">{m.inv_label_updated()}</dt>
						<dd>{new Date(investigation.updated_at).toLocaleString()}</dd>
					</div>
					{#if investigation.closed_at}
						<div class="flex justify-between">
							<dt class="opacity-60">{m.inv_label_closed()}</dt>
							<dd>{new Date(investigation.closed_at).toLocaleString()}</dd>
						</div>
					{/if}
					{#if investigation.thehive_case_id}
						<div class="flex justify-between">
							<dt class="opacity-60">{m.inv_label_thehive_case()}</dt>
							<dd class="font-mono text-sm">{investigation.thehive_case_id}</dd>
						</div>
					{/if}
					{#if investigation.threat_actor}
						<div class="flex justify-between">
							<dt class="opacity-60">{m.inv_label_threat_actor()}</dt>
							<dd class="badge variant-filled-error">{investigation.threat_actor}</dd>
						</div>
					{/if}
				</dl>
			</div>

			<!-- Verdict -->
			{#if investigation.verdict_decision}
				<div class="card p-4">
					<h3 class="h4 mb-4">{m.inv_col_verdict()}</h3>
					<div class="space-y-3">
						<div class="flex items-center justify-between">
							<span class="opacity-60">{m.inv_label_decision()}</span>
							<span class="badge {getVerdictBadge(investigation.verdict_decision)} text-lg px-3 py-1">
								{formatDecision(investigation.verdict_decision)}
							</span>
						</div>
						{#if investigation.verdict_confidence}
							<div>
								<div class="flex justify-between text-sm mb-1">
									<span class="opacity-60">{m.inv_label_confidence()}</span>
									<span>{(investigation.verdict_confidence * 100).toFixed(0)}%</span>
								</div>
								<div class="w-full h-2 bg-surface-500/30 rounded-full overflow-hidden">
									<div
										class="h-full rounded-full transition-all duration-300
											{investigation.verdict_confidence > 0.8 ? 'bg-success-500' :
											 investigation.verdict_confidence > 0.5 ? 'bg-warning-500' : 'bg-error-500'}"
										style="width: {investigation.verdict_confidence * 100}%"
									></div>
								</div>
							</div>
						{/if}
						{#if investigation.verdict_reasoning}
							<div>
								<span class="opacity-60 text-sm">{m.inv_label_reasoning()}</span>
								<p class="mt-1 text-sm bg-surface-500/20 rounded p-2">
									{investigation.verdict_reasoning}
								</p>
							</div>
						{/if}
					</div>
				</div>
			{/if}

			<!-- Agent Run (LangGraph) -->
			{#if investigation.tokens_used !== null && investigation.tokens_used !== undefined}
				{#if !$isCustomerScope}
					<div class="card p-4">
						<h3 class="h4 mb-4">{m.inv_agent_run()}</h3>
						<div class="space-y-3">
							<div>
								<div class="flex justify-between text-sm mb-1">
									<span class="opacity-60">{m.inv_token_spend()}</span>
									<span class="font-mono">
										{investigation.tokens_used?.toLocaleString() ?? 0}
										{#if investigation.tokens_budget}
											/ {investigation.tokens_budget.toLocaleString()}
										{/if}
									</span>
								</div>
								{#if investigation.tokens_budget}
									{@const ratio = Math.min(1, (investigation.tokens_used ?? 0) / investigation.tokens_budget)}
									<div class="w-full h-2 bg-surface-500/30 rounded-full overflow-hidden">
										<div
											class="h-full rounded-full transition-all duration-300
												{ratio > 0.8 ? 'bg-error-500' : ratio > 0.5 ? 'bg-warning-500' : 'bg-success-500'}"
											style="width: {ratio * 100}%"
										></div>
									</div>
								{/if}
							</div>
							{#if investigation.disposition}
								<div class="flex items-center justify-between">
									<span class="opacity-60 text-sm">{m.inv_label_disposition()}</span>
									<span class="badge {investigation.disposition === 'escalate' ? 'variant-filled-error' : investigation.disposition === 'close_fp' ? 'variant-filled-success' : investigation.disposition === 'halted_budget' ? 'variant-filled-warning' : 'variant-filled-surface'}">
										{investigation.disposition.replace('_', ' ')}
									</span>
								</div>
							{/if}

							{#if investigation.dollars_budget}
								<div class="flex justify-between text-sm">
									<span class="opacity-60">Cost</span>
									<span class="font-mono tabular-nums" data-testid="run-dollar-spend">
										{money(investigation.dollars_used ?? 0)} / {money(investigation.dollars_budget)}
									</span>
								</div>
							{/if}

							{#if investigation.disposition === 'halted_budget' && investigation.run_id}
								<div class="border-t border-surface-500/20 pt-3" data-testid="budget-unlock">
									<p class="text-sm opacity-70 mb-2">
										This run stopped at its cost ceiling. Until it is unlocked, the
										investigation cannot start another run.
									</p>
									{#if !unlockOpen}
										<button
											class="btn btn-sm variant-filled-warning"
											on:click={() => {
												unlockOpen = true;
												unlockError = null;
												unlockDollars = '';
											}}
											data-testid="budget-unlock-open"
										>
											Raise ceiling and resume
										</button>
									{:else}
										<div class="flex items-end gap-2 flex-wrap">
											<label class="label flex-1 min-w-40">
												<span class="text-xs opacity-70">
													New ceiling (must exceed {money(investigation.dollars_used ?? 0)} spent)
												</span>
												<input
													class="input input-sm font-mono"
													type="text"
													inputmode="decimal"
													bind:value={unlockDollars}
													disabled={unlockBusy}
													data-testid="budget-unlock-input"
												/>
											</label>
											<button
												class="btn btn-sm variant-filled-primary"
												on:click={unlockRun}
												disabled={unlockBusy}
												data-testid="budget-unlock-confirm"
											>
												{unlockBusy ? 'Resuming…' : 'Resume'}
											</button>
											<button
												class="btn btn-sm variant-soft"
												on:click={() => (unlockOpen = false)}
												disabled={unlockBusy}
											>
												Cancel
											</button>
										</div>
										{#if unlockError}
											<p class="text-error-500 text-xs mt-2" data-testid="budget-unlock-error">
												{unlockError}
											</p>
										{/if}
										<p class="text-xs opacity-50 mt-2">
											The run resumes where it stopped; spend already recorded is kept.
										</p>
									{/if}
								</div>
							{/if}
						</div>
					</div>
				{/if}
			{/if}

			<!-- Observable Stats -->
			<div class="card p-4">
				<h3 class="h4 mb-4">{m.inv_observable_summary()}</h3>
				<div class="space-y-2">
					<div class="flex items-center justify-between">
						<span>{m.inv_total()}</span>
						<span class="font-mono">{investigation.observable_count}</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="flex items-center gap-2">
							<span class="w-2 h-2 rounded-full bg-error-500"></span>
							{m.inv_col_malicious()}
						</span>
						<span class="font-mono text-error-500">{investigation.malicious_count}</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="flex items-center gap-2">
							<span class="w-2 h-2 rounded-full bg-warning-500"></span>
							{m.inv_suspicious()}
						</span>
						<span class="font-mono text-warning-500">{investigation.suspicious_count}</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="flex items-center gap-2">
							<span class="w-2 h-2 rounded-full bg-success-500"></span>
							{m.inv_clean()}
						</span>
						<span class="font-mono text-success-500">{investigation.clean_count}</span>
					</div>
				</div>
			</div>
		</div>

		<!-- Right Column: Event Timeline / Replay (#72) -->
		<div class="lg:col-span-2">
			<div class="card p-4">
					<div class="flex items-center justify-between mb-4">
						<div class="flex items-center gap-3">
							<h3 class="h4">{m.inv_event_timeline()}</h3>
							<SegmentedTabs
								value={eventView}
								options={[
									{ id: 'timeline', label: m.replay_view_timeline(), testid: 'view-timeline' },
									{ id: 'replay', label: m.replay_view_replay(), testid: 'view-replay' }
								]}
								on:change={(e) => (eventView = e.detail.id === 'replay' ? 'replay' : 'timeline')}
							/>
						</div>
						{#if eventView === 'timeline'}
							<button class="btn btn-sm variant-soft" on:click={loadEvents} disabled={eventsLoading}>
								{#if eventsLoading}
									<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
								{/if}
								{m.inv_refresh()}
							</button>
						{/if}
					</div>

				{#if eventView === 'replay'}
					{#key investigationId}
						<ReplayPanel
							{investigationId}
							isActive={investigation.status === 'in_progress' || investigation.status === 'pending'}
						/>
					{/key}
				{:else if eventsLoading && events.length === 0}
					<div class="flex items-center justify-center py-8">
						<div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500"></div>
					</div>
				{:else if events.length === 0}
					<p class="opacity-60 text-center py-8">{m.inv_no_events()}</p>
				{:else}
					<div class="space-y-4 max-h-[600px] overflow-y-auto pr-2">
						{#each events as event, i}
							{@const details = getEventDetails(event.event_type, event.data)}
							<div class="flex gap-3">
								<!-- Timeline Line -->
								<div class="flex flex-col items-center">
									<div class="w-8 h-8 rounded-full bg-surface-500/30 flex items-center justify-center {getEventColor(event.event_type)}">
										<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getEventIcon(event.event_type)} />
										</svg>
									</div>
									{#if i < events.length - 1}
										<div class="w-0.5 h-full min-h-8 bg-surface-500/30 my-1"></div>
									{/if}
								</div>

								<!-- Event Content -->
								<div class="flex-1 pb-4">
									<div class="flex items-center gap-2 mb-1">
										<span class="badge variant-soft text-xs">{formatEventType(event.event_type)}</span>
										<span class="text-xs opacity-60">
											{new Date(event.timestamp).toLocaleString()}
										</span>
									</div>
									<!-- Human-readable summary -->
									<p class="text-sm font-medium mb-2">{formatEventSummary(event.event_type, event.data)}</p>

									<!-- Structured details if available -->
									{#if details.length > 0}
										<div class="flex flex-wrap gap-x-4 gap-y-1 text-xs mb-2">
											{#each details as detail}
												{#if detail.label}
													<span class="opacity-60">{detail.label}:</span>
												{/if}
												<span class={detail.highlight ? 'text-error-500 font-semibold' : ''}>{detail.value}</span>
											{/each}
										</div>
									{/if}

									<!-- Expandable JSON details -->
									<button
										class="text-xs opacity-60 hover:opacity-100 flex items-center gap-1 transition-opacity"
										on:click={() => toggleEventDetails(event.id)}
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											class="h-3 w-3 transition-transform {expandedEvents.has(event.id) ? 'rotate-90' : ''}"
											fill="none"
											viewBox="0 0 24 24"
											stroke="currentColor"
										>
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
										</svg>
										{expandedEvents.has(event.id) ? m.inv_hide_raw() : m.inv_show_raw()}
									</button>
									{#if expandedEvents.has(event.id)}
										<div class="mt-2 text-sm bg-surface-500/10 rounded p-3 border border-surface-500/20">
											<pre class="text-xs overflow-x-auto whitespace-pre-wrap font-mono">{JSON.stringify(event.data, null, 2)}</pre>
										</div>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<!-- Cancel Modal -->
{#if showCancelModal}
	<div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
		<div class="card p-6 w-full max-w-md m-4">
			<h3 class="h3 mb-4">{m.inv_cancel_investigation()}</h3>
			<p class="mb-4 opacity-80">{m.inv_cancel_confirm()}</p>
			<label class="label mb-4">
				<span>{m.inv_reason_optional()}</span>
				<textarea
					class="textarea"
					rows="3"
					bind:value={cancelReason}
					placeholder={m.inv_reason_placeholder()}
				></textarea>
			</label>
			<div class="flex justify-end gap-2">
				<button
					class="btn variant-soft"
					on:click={() => { showCancelModal = false; cancelReason = ''; }}
				>
					{m.inv_keep_investigation()}
				</button>
					<button
						class="btn variant-filled-error"
						disabled={actionLoading}
						on:click={handleCancel}
					>
						{#if actionLoading}
							<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
						{/if}
						{m.inv_cancel_investigation()}
					</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.chat-launcher {
		position: fixed;
		bottom: 1.5rem;
		right: 1.5rem;
		z-index: 60;
		box-shadow: 0 8px 20px rgba(0, 0, 0, 0.25);
	}
	.chat-dock {
		position: fixed;
		bottom: 5rem;
		right: 1.5rem;
		width: min(420px, calc(100vw - 3rem));
		height: min(70vh, 640px);
		z-index: 55;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
		border-radius: 0.75rem;
		overflow: hidden;
	}
	@media (max-width: 640px) {
		.chat-dock {
			right: 0.75rem;
			bottom: 4.5rem;
			width: calc(100vw - 1.5rem);
		}
	}
</style>
