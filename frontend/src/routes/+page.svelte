<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, type MetricsOverview, type HourlyMetricsResponse, type InvestigationSummary } from '$lib/api/client';
	import { recentEvents } from '$lib/stores';
	import { browser } from '$app/environment';
	import { formatDecision, formatAction, formatDuration, formatPhase, formatStatus, formatSeverity } from '$lib/utils/formatters';

	let metrics: MetricsOverview | null = null;
	let hourlyData: HourlyMetricsResponse | null = null;
	let recentInvestigations: InvestigationSummary[] = [];
	let activeInvestigations: InvestigationSummary[] = [];
	let loading = true;
	let error: string | null = null;

	// ApexCharts instance
	let chartElement: HTMLDivElement;
	let chart: any = null;

	// Verdict bar chart data
	$: verdictData = metrics ? Object.entries(metrics.verdict_breakdown).map(([name, value]) => ({
		name,
		value: value as number
	})) : [];

	// Initialize chart reactively when element and data are both ready
	$: if (chartElement && hourlyData && browser) {
		initChart();
	}

	async function initChart() {
		if (!browser || !hourlyData || !chartElement) return;

		const ApexCharts = (await import('apexcharts')).default;

		const categories = hourlyData.metrics.map(m => new Date(m.hour).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));

		// Stacked bar data
		const createdData = hourlyData.metrics.map(m => m.investigations_created);
		const manualClosedData = hourlyData.metrics.map(m => Math.max(0, m.investigations_closed - m.auto_closed));
		const autoClosedData = hourlyData.metrics.map(m => m.auto_closed);
		const escalatedData = hourlyData.metrics.map(m => m.escalations);

		// Use backend-computed open_wip (accurate backlog at end of each hour)
		const backlogData = hourlyData.metrics.map(m => m.open_wip);

		const options = {
			chart: {
				type: 'bar',
				height: 280,
				stacked: true,
				stackOnlyBar: true,
				background: 'transparent',
				toolbar: { show: false },
				animations: {
					enabled: true,
					easing: 'easeinout',
					speed: 600
				},
				fontFamily: 'inherit'
			},
			series: [
				{ name: 'Created', type: 'bar', group: 'inflow', data: createdData },
				{ name: 'Manual Close', type: 'bar', group: 'outflow', data: manualClosedData },
				{ name: 'Auto-Close', type: 'bar', group: 'outflow', data: autoClosedData },
				{ name: 'Escalated', type: 'bar', group: 'outflow', data: escalatedData },
				{ name: 'Open (Backlog)', type: 'line', data: backlogData }
			],
			colors: ['#6366f1', '#22c55e', '#14b8a6', '#f59e0b', '#ef4444'],
			plotOptions: {
				bar: {
					columnWidth: '70%',
					borderRadius: 2
				}
			},
			stroke: {
				width: [0, 0, 0, 0, 3],
				curve: 'smooth'
			},
			fill: {
				opacity: [1, 1, 1, 1, 1]
			},
			dataLabels: { enabled: false },
			xaxis: {
				categories,
				labels: {
					style: { colors: '#94a3b8', fontSize: '10px' },
					rotate: -45,
					rotateAlways: false,
					hideOverlappingLabels: true
				},
				axisBorder: { show: false },
				axisTicks: { show: false }
			},
			yaxis: [
				{
					seriesName: ['Created', 'Manual Close', 'Auto-Close', 'Escalated'],
					title: { text: 'Per Hour', style: { color: '#94a3b8', fontSize: '11px' } },
					labels: {
						style: { colors: '#94a3b8', fontSize: '11px' },
						formatter: (val: number) => Math.round(val).toString()
					}
				},
				{
					seriesName: 'Open (Backlog)',
					opposite: true,
					title: { text: 'Backlog', style: { color: '#ef4444', fontSize: '11px' } },
					labels: {
						style: { colors: '#ef4444', fontSize: '11px' },
						formatter: (val: number) => Math.round(val).toString()
					}
				}
			],
			grid: {
				borderColor: '#334155',
				strokeDashArray: 4,
				xaxis: { lines: { show: false } }
			},
			legend: {
				position: 'top',
				horizontalAlign: 'right',
				labels: { colors: '#94a3b8' },
				markers: { radius: 2 },
				fontSize: '11px'
			},
			tooltip: {
				theme: 'dark',
				shared: true,
				intersect: false
			}
		};

			try {
				if (chart) {
					chart.destroy();
				}
				chart = new ApexCharts(chartElement, options);
				await chart.render();
			} catch (e) {
				if (import.meta.env.DEV) console.error('ApexCharts render error:', e);
			}
		}

		async function loadDashboard() {
			loading = true;
			error = null;
			try {
				const [metricsRes, hourlyRes, investigationsRes] = await Promise.all([
					api.metrics.overview(),
					api.metrics.hourly(24),
					api.investigations.list({ page_size: 10 })
				]);
				metrics = metricsRes;
				hourlyData = hourlyRes;
				recentInvestigations = investigationsRes.items;

				// Filter for active investigations
				activeInvestigations = investigationsRes.items.filter(
					inv => ['pending', 'in_progress', 'paused'].includes(inv.status)
				);

			} catch (e) {
				if (import.meta.env.DEV) console.error('Failed to load dashboard:', e);
				error = e instanceof Error ? e.message : 'Failed to load metrics';
			} finally {
				loading = false;
			}
		}

		onMount(() => {
			loadDashboard();
		});

	onDestroy(() => {
		if (chart) {
			chart.destroy();
		}
	});

	function getSeverityColor(severity: string): string {
		switch (severity?.toLowerCase()) {
			case 'critical':
				return 'variant-filled-error';
			case 'high':
				return 'variant-filled-warning';
			case 'medium':
				return 'variant-filled-secondary';
			case 'low':
				return 'variant-filled-tertiary';
			default:
				return 'variant-filled-surface';
		}
	}

	function getStatusColor(status: string): string {
		switch (status) {
			case 'pending':
				return 'variant-soft-warning';
			case 'in_progress':
				return 'variant-soft-primary';
			case 'paused':
				return 'variant-soft-tertiary';
			case 'closed':
			case 'auto_closed':
				return 'variant-soft-success';
			case 'escalated':
			case 'rejected':
				return 'variant-soft-error';
			case 'cancelled':
				return 'variant-soft';
			default:
				return 'variant-soft';
		}
	}

	function getPhaseIcon(phase: string): string {
		switch (phase) {
			case 'triage':
				return 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2';
			case 'enrichment':
				return 'M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z';
			case 'analysis':
				return 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z';
			case 'verdict':
				return 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z';
			case 'human_review':
				return 'M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z';
			default:
				return 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z';
		}
	}

	function formatEventType(type: string): { label: string; variant: string } {
		const mapping: Record<string, { label: string; variant: string }> = {
			'investigation.created': { label: 'Investigation Started', variant: 'variant-soft-primary' },
			'investigation.closed': { label: 'Investigation Closed', variant: 'variant-soft-success' },
			'human.review_requested': { label: 'Review Requested', variant: 'variant-soft-warning' },
			'human.decision_received': { label: 'Review Completed', variant: 'variant-soft-success' },
			'verdict.rendered': { label: 'Verdict Rendered', variant: 'variant-soft-tertiary' },
			'enrichment.completed': { label: 'Enrichment Done', variant: 'variant-soft' },
			'enrichment.requested': { label: 'Enrichment Started', variant: 'variant-soft' },
			'enrichment.failed': { label: 'Enrichment Failed', variant: 'variant-soft-error' },
			'thehive.case_created': { label: 'Case Created', variant: 'variant-soft-success' },
			'phase.changed': { label: 'Phase Changed', variant: 'variant-soft' },
			'alert.correlated': { label: 'Alert Added', variant: 'variant-soft' },
			'observable.extracted': { label: 'Observable Found', variant: 'variant-soft' },
			'supervisor.decision': { label: 'Supervisor Decision', variant: 'variant-soft-tertiary' },
			'misp.context_retrieved': { label: 'MISP Intel Retrieved', variant: 'variant-soft' },
			'wazuh.forensics_collected': { label: 'Forensics Collected', variant: 'variant-soft' },
		};
		return mapping[type] || { label: type.replace(/[._]/g, ' ').replace(/\b\w/g, c => c.toUpperCase()), variant: 'variant-soft' };
	}

	function formatEventDetails(event: { type: string; data: Record<string, unknown> }): string {
		const d = event.data;
		switch (event.type) {
			case 'investigation.created':
				return d.title ? `"${d.title}"` : 'New investigation started';
			case 'investigation.closed':
				return d.verdict_decision ? formatDecision(d.verdict_decision as string) : 'Investigation closed';
			case 'human.review_requested':
				const reviewVerdict = d.verdict_decision ? formatDecision(d.verdict_decision as string) : '';
				const reviewConf = d.verdict_confidence ? ` (${Math.round((d.verdict_confidence as number) * 100)}%)` : '';
				return reviewVerdict ? `Suggested: ${reviewVerdict}${reviewConf}` : 'Awaiting review';
			case 'human.decision_received':
				return d.decision ? `Analyst: ${formatDecision(d.decision as string)}` : 'Review submitted';
			case 'verdict.rendered':
				const conf = d.confidence ? ` (${Math.round((d.confidence as number) * 100)}%)` : '';
				return d.decision ? `${formatDecision(d.decision as string)}${conf}` : 'Verdict determined';
			case 'enrichment.completed':
				return d.analyzer ? `${d.analyzer}: ${d.verdict || 'done'}` : 'Enrichment complete';
			case 'enrichment.requested':
				return d.analyzer ? `Running ${d.analyzer}` : 'Starting enrichment';
			case 'enrichment.failed':
				return d.analyzer ? `${d.analyzer} failed` : 'Enrichment error';
			case 'thehive.case_created':
				return d.case_number ? `Case #${d.case_number}` : 'Case opened in TheHive';
			case 'phase.changed':
				return d.to_phase ? `Now in ${formatPhase(d.to_phase as string)} phase` : 'Phase updated';
			case 'alert.correlated':
				return d.rule_description ? `${d.rule_description}` : 'Alert correlated';
			case 'observable.extracted':
				return d.value ? `${d.type}: ${d.value}` : 'Observable detected';
			case 'supervisor.decision':
				return d.action ? formatAction(d.action as string) : 'Decision recorded';
			case 'misp.context_retrieved':
				return d.event_count ? `${d.event_count} related events found` : 'Threat intel retrieved';
			case 'wazuh.forensics_collected':
				return d.agent_id ? `Agent ${d.agent_id}` : 'Forensic data collected';
			default:
				// Fallback: show first meaningful field value
				const firstValue = Object.values(d).find(v => typeof v === 'string' && v.length < 60);
				return firstValue ? String(firstValue) : 'Event recorded';
		}
	}
</script>

<svelte:head>
	<title>Dashboard - SocTalk</title>
</svelte:head>

<div class="flex items-center justify-between mb-6">
	<h1 class="h2">Dashboard</h1>
	<button class="btn variant-soft" on:click={loadDashboard} disabled={loading}>
		{#if loading}
			<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
		{/if}
		Refresh
	</button>
</div>

{#if loading}
	<div class="flex items-center justify-center h-64">
		<div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500"></div>
	</div>
{:else if error}
	<div class="alert variant-filled-error">
		<span>Error: {error}</span>
	</div>
{:else if metrics}
	<!-- KPI Cards -->
	<div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
		<div class="card p-4 variant-soft">
			<h3 class="text-sm opacity-60">Open Investigations</h3>
			<p class="text-3xl font-bold">{metrics.open_investigations}</p>
		</div>
		<div class="card p-4 variant-soft-warning">
			<h3 class="text-sm opacity-60">Pending Reviews</h3>
			<p class="text-3xl font-bold">{metrics.pending_reviews}</p>
		</div>
		<div class="card p-4 variant-soft">
			<h3 class="text-sm opacity-60">Avg. Time to Triage</h3>
			<p class="text-3xl font-bold">{formatDuration(metrics.avg_time_to_triage_seconds)}</p>
		</div>
		<div class="card p-4 variant-soft">
			<h3 class="text-sm opacity-60">Avg. Time to Verdict</h3>
			<p class="text-3xl font-bold">{formatDuration(metrics.avg_time_to_verdict_seconds)}</p>
		</div>
	</div>

	<!-- Today's Activity -->
	<div class="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-6">
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Created Today</h4>
			<p class="text-xl font-bold">{metrics.investigations_created_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Closed Today</h4>
			<p class="text-xl font-bold">{metrics.investigations_closed_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Escalations</h4>
			<p class="text-xl font-bold">{metrics.escalations_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Auto-Closed</h4>
			<p class="text-xl font-bold">{metrics.auto_closed_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">Malicious IOCs</h4>
			<p class="text-xl font-bold text-error-500">{metrics.malicious_observables_today}</p>
		</div>
	</div>

	<!-- Charts Row -->
	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
		<!-- Investigation Throughput Chart -->
		<div class="card p-4 lg:col-span-2">
			<h3 class="h4 mb-4">Investigation Throughput (24h)</h3>
			<div bind:this={chartElement} class="h-64"></div>
			{#if hourlyData && hourlyData.metrics.length === 0}
				<p class="opacity-60 text-center py-8">No hourly data available</p>
			{/if}
		</div>

		<!-- Verdict Distribution -->
		<div class="card p-4">
			<h3 class="h4 mb-4">Verdicts Today</h3>
			{#if verdictData.length > 0}
				<div class="space-y-3 py-4">
					{#each verdictData as { name, value }}
						{@const total = verdictData.reduce((acc, v) => acc + v.value, 0)}
						{@const percentage = total > 0 ? (value / total) * 100 : 0}
						<div class="space-y-1">
							<div class="flex justify-between text-sm">
								<span class="font-medium">{formatDecision(name)}</span>
								<span class="font-mono">{value}</span>
							</div>
							<div class="w-full h-3 bg-surface-500/30 rounded-full overflow-hidden">
						<div
									class="h-full rounded-full transition-all duration-300
										{name === 'escalate' ? 'bg-error-500' :
										 name === 'needs_more_info' || name === 'suspicious' ? 'bg-warning-500' :
										 name === 'close' || name === 'auto_close' ? 'bg-success-500' : 'bg-surface-500'}"
									style="width: {percentage}%"
								></div>
							</div>
						</div>
					{/each}
				</div>
				<div class="flex flex-wrap justify-center gap-3 mt-4 text-xs">
					<span class="flex items-center gap-1">
						<span class="w-2 h-2 rounded bg-error-500"></span> Escalate
					</span>
					<span class="flex items-center gap-1">
						<span class="w-2 h-2 rounded bg-warning-500"></span> Needs more info
					</span>
					<span class="flex items-center gap-1">
						<span class="w-2 h-2 rounded bg-success-500"></span> Close
					</span>
				</div>
			{:else}
				<p class="opacity-60 text-center py-8">No verdicts yet today</p>
			{/if}
		</div>
	</div>

	<!-- Active Investigations Status Board -->
	<div class="card p-4 mb-6">
		<h3 class="h4 mb-4">Active Investigations</h3>
		{#if activeInvestigations.length > 0}
			<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
				{#each activeInvestigations as inv}
					<a href="/investigations/{inv.id}" class="card p-3 variant-soft hover:variant-soft-primary transition-colors">
						<div class="flex items-start justify-between mb-2">
							<div class="flex items-center gap-2">
								<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5 opacity-60" fill="none" viewBox="0 0 24 24" stroke="currentColor">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getPhaseIcon(inv.phase)} />
								</svg>
								<span class="badge {getStatusColor(inv.status)} text-xs">{formatStatus(inv.status)}</span>
							</div>
							{#if inv.max_severity}
								<span class="badge {getSeverityColor(inv.max_severity)} text-xs">{formatSeverity(inv.max_severity)}</span>
							{/if}
						</div>
						<h4 class="font-medium text-sm truncate">{inv.title || 'Untitled Investigation'}</h4>
						<div class="flex items-center gap-3 mt-2 text-xs opacity-60">
							<span>Phase: {formatPhase(inv.phase)}</span>
							<span>{inv.alert_count} alerts</span>
							{#if inv.malicious_count > 0}
								<span class="text-error-500">{inv.malicious_count} malicious</span>
							{/if}
						</div>
					</a>
				{/each}
			</div>
		{:else}
			<p class="opacity-60 text-center py-8">No active investigations</p>
		{/if}
	</div>

	<!-- Bottom Row: Recent Investigations + Severity Breakdown -->
	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
		<!-- Recent Investigations Table -->
		<div class="card p-4 lg:col-span-2">
			<h3 class="h4 mb-4">Recent Investigations</h3>
			{#if recentInvestigations.length > 0}
				<div class="table-container">
					<table class="table table-compact">
						<thead>
							<tr>
								<th>Title</th>
								<th>Status</th>
								<th>Verdict</th>
								<th>Alerts</th>
								<th>Created</th>
							</tr>
						</thead>
						<tbody>
							{#each recentInvestigations.slice(0, 5) as inv}
								<tr>
									<td class="max-w-xs truncate">
										<a href="/investigations/{inv.id}" class="anchor">
											{inv.title || 'Untitled'}
										</a>
									</td>
									<td><span class="badge {getStatusColor(inv.status)} text-xs">{formatStatus(inv.status)}</span></td>
									<td>
										{#if inv.verdict_decision}
											<span class="badge text-xs {inv.verdict_decision === 'escalate' ? 'variant-filled-error' : inv.verdict_decision === 'suspicious' ? 'variant-filled-warning' : 'variant-filled-success'}">
												{formatDecision(inv.verdict_decision)}
											</span>
										{:else}
											<span class="opacity-40">-</span>
										{/if}
									</td>
									<td>{inv.alert_count}</td>
									<td class="text-xs opacity-60">
										{new Date(inv.created_at).toLocaleString()}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				<div class="mt-3 text-right">
					<a href="/investigations" class="anchor text-sm">View all investigations &rarr;</a>
				</div>
			{:else}
				<p class="opacity-60 text-center py-8">No investigations yet</p>
			{/if}
		</div>

		<!-- Severity Breakdown -->
		<div class="card p-4">
			<h3 class="h4 mb-4">Open by Severity</h3>
			<div class="space-y-3">
				{#each Object.entries(metrics.severity_breakdown) as [severity, count]}
					<div class="flex items-center justify-between">
						<span class="badge {getSeverityColor(severity)}">{formatSeverity(severity)}</span>
						<div class="flex items-center gap-2">
							<div class="w-24 h-2 bg-surface-500 rounded overflow-hidden">
								<div
									class="h-full {severity === 'critical' ? 'bg-error-500' : severity === 'high' ? 'bg-warning-500' : 'bg-secondary-500'}"
									style="width: {(count / metrics.open_investigations) * 100}%"
								></div>
							</div>
							<span class="font-mono text-sm">{count}</span>
						</div>
					</div>
				{/each}
				{#if Object.keys(metrics.severity_breakdown).length === 0}
					<p class="opacity-60 text-center py-4">No open investigations</p>
				{/if}
			</div>
		</div>
	</div>

	<!-- Recent Events -->
	<div class="card p-4">
		<h3 class="h4 mb-4">Live Event Stream</h3>
		{#if $recentEvents.length > 0}
			<div class="table-container">
				<table class="table table-compact">
					<thead>
						<tr>
							<th>Time</th>
							<th>Event</th>
							<th>Details</th>
						</tr>
					</thead>
					<tbody>
						{#each $recentEvents.slice(0, 10) as event}
							{@const formatted = formatEventType(event.type)}
							<tr>
								<td class="text-xs opacity-60 whitespace-nowrap">
									{new Date(event.timestamp).toLocaleTimeString()}
								</td>
								<td>
									<span class="badge {formatted.variant} text-xs">{formatted.label}</span>
								</td>
								<td class="text-sm truncate max-w-md">
									{formatEventDetails(event)}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="opacity-60 text-center py-8">No events yet. Events will appear here in real-time.</p>
		{/if}
	</div>
{/if}
