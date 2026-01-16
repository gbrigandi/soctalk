<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, type Investigation, type InvestigationTimelineEvent } from '$lib/api/client';
	import { addToast } from '$lib/stores';
	import { formatStatus, formatPhase, formatSeverity, formatDecision, formatDuration, formatEventType } from '$lib/utils/formatters';

	let investigation: Investigation | null = null;
	let events: InvestigationTimelineEvent[] = [];
	let loading = true;
	let eventsLoading = true;
	let error: string | null = null;
	let actionLoading = false;
	let showCancelModal = false;
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
				return `Investigation started: "${data.title || 'Untitled'}"`;
			case 'investigation.started':
				return `Investigation began in ${formatPhase(data.phase as string) || 'Triage'} phase`;
			case 'investigation.closed':
				return `Investigation closed`;
			case 'investigation.paused':
				return `Investigation paused`;
			case 'investigation.resumed':
				return `Investigation resumed`;
			case 'investigation.cancelled':
				return `Investigation cancelled${data.reason ? `: ${data.reason}` : ''}`;
			case 'investigation.escalated':
				return `Investigation escalated to incident response`;
			case 'investigation.auto_closed':
				return `Investigation auto-closed (no threats found)`;
			case 'alert.added':
				return `Alert added: ${data.alert_id || 'Unknown'}`;
			case 'alert.correlated':
				return `Alert correlated: ${data.description || data.alert_id || 'Unknown alert'}`;
			case 'observable.extracted':
				return `Found ${data.observable_type}: ${data.observable_value}`;
			case 'enrichment.requested':
				return `Enrichment requested from ${data.enrichment_type || 'external source'}`;
			case 'enrichment.completed': {
				const result = data.result as Record<string, number> | undefined;
				if (result && typeof result.malicious === 'number') {
					return `${data.enrichment_type}: ${result.malicious} malicious, ${result.suspicious || 0} suspicious detections for ${data.observable_value}`;
				}
				return `Enrichment completed for ${data.observable_value}`;
			}
			case 'enrichment.failed':
				return `Enrichment failed for ${data.observable_value}: ${data.error || 'Unknown error'}`;
			case 'phase.changed':
				return `Phase changed: ${formatPhase(data.old_phase as string) || '?'} → ${formatPhase(data.new_phase as string || data.phase as string) || '?'}`;
			case 'verdict.rendered':
			case 'verdict.proposed':
				return `Verdict: ${formatDecision(data.decision as string)} (${Math.round((data.confidence as number || 0) * 100)}% confidence)`;
			case 'human.review_requested':
			case 'review.requested':
				return `Human review requested: ${data.reason || 'Manual review required'}`;
			case 'human.decision_received':
				return `Human decision: ${formatDecision(data.decision as string)}`;
			case 'thehive.case_created':
				return `TheHive case created: ${data.case_id || 'Unknown'}`;
			case 'thehive.alert_promoted':
				return `Alert promoted to TheHive case`;
			case 'misp.ioc_matched':
				return `MISP IOC match found`;
			case 'misp.context_retrieved':
				return `MISP context retrieved`;
			case 'analyzer.invoked':
				return `Analyzer invoked: ${data.analyzer || 'Unknown'}`;
			case 'analyzer.completed':
				return `Analyzer completed: ${data.analyzer || 'Unknown'}`;
			case 'error.occurred':
				return `Error: ${data.message || data.error || 'Unknown error'}`;
			default:
				return formatEventType(eventType);
		}
	}

	function getEventDetails(eventType: string, data: Record<string, unknown>): Array<{label: string, value: string, highlight?: boolean}> {
		const details: Array<{label: string, value: string, highlight?: boolean}> = [];

		switch (eventType) {
			case 'investigation.created':
				if (data.alert_ids) details.push({ label: 'Alerts', value: `${(data.alert_ids as string[]).length} alerts` });
				if (data.source_ip) details.push({ label: 'Source IP', value: String(data.source_ip) });
				if (data.source_agent) details.push({ label: 'Agent', value: String(data.source_agent) });
				if (data.max_severity) details.push({ label: 'Severity', value: String(data.max_severity).toUpperCase(), highlight: true });
				break;
			case 'alert.correlated':
				if (data.rule_id) details.push({ label: 'Rule ID', value: String(data.rule_id) });
				if (data.severity) details.push({ label: 'Severity', value: String(data.severity).toUpperCase(), highlight: true });
				if (data.description) details.push({ label: 'Description', value: String(data.description) });
				break;
			case 'observable.extracted':
				if (data.classification) details.push({ label: 'Classification', value: String(data.classification) });
				break;
			case 'enrichment.completed': {
				const result = data.result as Record<string, number> | undefined;
				if (result) {
					if (typeof result.malicious === 'number') details.push({ label: 'Malicious', value: String(result.malicious), highlight: result.malicious > 0 });
					if (typeof result.suspicious === 'number') details.push({ label: 'Suspicious', value: String(result.suspicious) });
					if (typeof result.harmless === 'number') details.push({ label: 'Harmless', value: String(result.harmless) });
				}
				break;
			}
			case 'verdict.rendered':
			case 'verdict.proposed':
				if (data.assessment) details.push({ label: 'Assessment', value: String(data.assessment) });
				if (data.recommendation) details.push({ label: 'Recommendation', value: String(data.recommendation) });
				if (data.evidence) {
					const evidence = data.evidence as string[];
					evidence.forEach((e, i) => details.push({ label: i === 0 ? 'Evidence' : '', value: e }));
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
			error = e instanceof Error ? e.message : 'Failed to load investigation';
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

	async function handlePause() {
		if (!investigationId) return;
		actionLoading = true;
		try {
			const result = await api.investigations.pause(investigationId);
			await Promise.all([refreshInvestigation(), loadEvents()]);
			addToast({ type: 'success', message: result.message });
		} catch (e) {
			addToast({ type: 'error', message: e instanceof Error ? e.message : 'Failed to pause' });
		} finally {
			actionLoading = false;
		}
	}

	async function handleResume() {
		if (!investigationId) return;
		actionLoading = true;
		try {
			const result = await api.investigations.resume(investigationId);
			await Promise.all([refreshInvestigation(), loadEvents()]);
			addToast({ type: 'success', message: result.message });
		} catch (e) {
			addToast({ type: 'error', message: e instanceof Error ? e.message : 'Failed to resume' });
		} finally {
			actionLoading = false;
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
			addToast({ type: 'error', message: e instanceof Error ? e.message : 'Failed to cancel' });
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
	<title>{investigation?.title || 'Investigation'} - SocTalk</title>
</svelte:head>

{#if loading}
	<div class="flex items-center justify-center h-64">
		<div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500"></div>
	</div>
{:else if error}
	<div class="alert variant-filled-error">
		<span>Error: {error}</span>
	</div>
{:else if investigation}
	<!-- Header -->
	<div class="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4 mb-6">
		<div>
			<div class="flex items-center gap-2 mb-2">
				<a href="/investigations" class="btn btn-sm variant-soft">
					<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
					</svg>
					Back
				</a>
			</div>
			<h1 class="h2">{investigation.title || 'Untitled Investigation'}</h1>
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
		<div class="flex gap-2">
			{#if investigation.status === 'in_progress'}
					<button
						class="btn variant-soft-warning"
						disabled={actionLoading}
						on:click={handlePause}
					>
						{#if actionLoading}
							<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
							Pause
						{:else}
							<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" />
							</svg>
							Pause
					{/if}
				</button>
			{:else if investigation.status === 'paused'}
					<button
						class="btn variant-soft-success"
						disabled={actionLoading}
						on:click={handleResume}
					>
						{#if actionLoading}
							<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
							Resume
						{:else}
							<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
							</svg>
							Resume
					{/if}
				</button>
			{/if}
			{#if ['pending', 'in_progress', 'paused'].includes(investigation.status)}
				<button
					class="btn variant-soft-error"
					disabled={actionLoading}
					on:click={() => showCancelModal = true}
				>
					<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
					Cancel
				</button>
			{/if}
		</div>
	</div>

	<!-- Info Cards -->
	<div class="grid grid-cols-2 lg:grid-cols-6 gap-4 mb-6">
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Alerts</h4>
			<p class="text-2xl font-bold">{investigation.alert_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Observables</h4>
			<p class="text-2xl font-bold">{investigation.observable_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60 text-error-500">Malicious</h4>
			<p class="text-2xl font-bold text-error-500">{investigation.malicious_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60 text-warning-500">Suspicious</h4>
			<p class="text-2xl font-bold text-warning-500">{investigation.suspicious_count}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Time to Triage</h4>
			<p class="text-2xl font-bold">{formatDuration(investigation.time_to_triage_seconds)}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Time to Verdict</h4>
			<p class="text-2xl font-bold">{formatDuration(investigation.time_to_verdict_seconds)}</p>
		</div>
	</div>

	<!-- Main Content Grid -->
	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
		<!-- Left Column: Details + Verdict -->
		<div class="space-y-6">
			<!-- Investigation Details -->
			<div class="card p-4">
				<h3 class="h4 mb-4">Details</h3>
				<dl class="space-y-2">
					<div class="flex justify-between">
						<dt class="opacity-60">ID</dt>
						<dd class="font-mono text-xs">{investigation.id.slice(0, 8)}...</dd>
					</div>
					<div class="flex justify-between">
						<dt class="opacity-60">Created</dt>
						<dd>{new Date(investigation.created_at).toLocaleString()}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="opacity-60">Updated</dt>
						<dd>{new Date(investigation.updated_at).toLocaleString()}</dd>
					</div>
					{#if investigation.closed_at}
						<div class="flex justify-between">
							<dt class="opacity-60">Closed</dt>
							<dd>{new Date(investigation.closed_at).toLocaleString()}</dd>
						</div>
					{/if}
					{#if investigation.thehive_case_id}
						<div class="flex justify-between">
							<dt class="opacity-60">TheHive Case</dt>
							<dd class="font-mono text-sm">{investigation.thehive_case_id}</dd>
						</div>
					{/if}
					{#if investigation.threat_actor}
						<div class="flex justify-between">
							<dt class="opacity-60">Threat Actor</dt>
							<dd class="badge variant-filled-error">{investigation.threat_actor}</dd>
						</div>
					{/if}
				</dl>
			</div>

			<!-- Verdict -->
			{#if investigation.verdict_decision}
				<div class="card p-4">
					<h3 class="h4 mb-4">Verdict</h3>
					<div class="space-y-3">
						<div class="flex items-center justify-between">
							<span class="opacity-60">Decision</span>
							<span class="badge {getVerdictBadge(investigation.verdict_decision)} text-lg px-3 py-1">
								{formatDecision(investigation.verdict_decision)}
							</span>
						</div>
						{#if investigation.verdict_confidence}
							<div>
								<div class="flex justify-between text-sm mb-1">
									<span class="opacity-60">Confidence</span>
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
								<span class="opacity-60 text-sm">Reasoning</span>
								<p class="mt-1 text-sm bg-surface-500/20 rounded p-2">
									{investigation.verdict_reasoning}
								</p>
							</div>
						{/if}
					</div>
				</div>
			{/if}

			<!-- Observable Stats -->
			<div class="card p-4">
				<h3 class="h4 mb-4">Observable Summary</h3>
				<div class="space-y-2">
					<div class="flex items-center justify-between">
						<span>Total</span>
						<span class="font-mono">{investigation.observable_count}</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="flex items-center gap-2">
							<span class="w-2 h-2 rounded-full bg-error-500"></span>
							Malicious
						</span>
						<span class="font-mono text-error-500">{investigation.malicious_count}</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="flex items-center gap-2">
							<span class="w-2 h-2 rounded-full bg-warning-500"></span>
							Suspicious
						</span>
						<span class="font-mono text-warning-500">{investigation.suspicious_count}</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="flex items-center gap-2">
							<span class="w-2 h-2 rounded-full bg-success-500"></span>
							Clean
						</span>
						<span class="font-mono text-success-500">{investigation.clean_count}</span>
					</div>
				</div>
			</div>
		</div>

		<!-- Right Column: Event Timeline -->
		<div class="lg:col-span-2">
			<div class="card p-4">
					<div class="flex items-center justify-between mb-4">
						<h3 class="h4">Event Timeline</h3>
						<button class="btn btn-sm variant-soft" on:click={loadEvents} disabled={eventsLoading}>
							{#if eventsLoading}
								<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
							{/if}
							Refresh
						</button>
					</div>

				{#if eventsLoading && events.length === 0}
					<div class="flex items-center justify-center py-8">
						<div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500"></div>
					</div>
				{:else if events.length === 0}
					<p class="opacity-60 text-center py-8">No events recorded</p>
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
										{expandedEvents.has(event.id) ? 'Hide' : 'Show'} raw data
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
			<h3 class="h3 mb-4">Cancel Investigation</h3>
			<p class="mb-4 opacity-80">Are you sure you want to cancel this investigation?</p>
			<label class="label mb-4">
				<span>Reason (optional)</span>
				<textarea
					class="textarea"
					rows="3"
					bind:value={cancelReason}
					placeholder="Provide a reason for cancellation..."
				></textarea>
			</label>
			<div class="flex justify-end gap-2">
				<button
					class="btn variant-soft"
					on:click={() => { showCancelModal = false; cancelReason = ''; }}
				>
					Keep Investigation
				</button>
					<button
						class="btn variant-filled-error"
						disabled={actionLoading}
						on:click={handleCancel}
					>
						{#if actionLoading}
							<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
						{/if}
						Cancel Investigation
					</button>
			</div>
		</div>
	</div>
{/if}
