<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { api, type MetricsOverview, type HourlyMetricsResponse, type InvestigationSummary } from '$lib/api/client';
	import { recentEvents, isMsspScope, authSession } from '$lib/stores';
	import { browser } from '$app/environment';
	import { formatDecision, formatAction, formatDuration, formatPhase, formatStatus, formatSeverity, EVENT_TYPE_MESSAGES } from '$lib/utils/formatters';
	import { m } from '$lib/paraglide/messages';
	import { localizeHref } from '$lib/i18n';
	import MsspDashboard from '$lib/components/MsspDashboard.svelte';
	import FleetPanel from '$lib/pipeline-viz/FleetPanel.svelte';

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

		// Localized series names — evaluated here (render time, never module
		// scope). The same strings are referenced by the yaxis seriesName
		// grouping below, so they must stay in sync.
		const seriesCreated = m.dash_series_created();
		const seriesManualClose = m.dash_series_manual_close();
		const seriesAutoClose = m.dash_series_auto_close();
		const seriesEscalated = m.dash_series_escalated();
		const seriesBacklog = m.dash_series_open_backlog();

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
				{ name: seriesCreated, type: 'bar', group: 'inflow', data: createdData },
				{ name: seriesManualClose, type: 'bar', group: 'outflow', data: manualClosedData },
				{ name: seriesAutoClose, type: 'bar', group: 'outflow', data: autoClosedData },
				{ name: seriesEscalated, type: 'bar', group: 'outflow', data: escalatedData },
				{ name: seriesBacklog, type: 'line', data: backlogData }
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
					seriesName: [seriesCreated, seriesManualClose, seriesAutoClose, seriesEscalated],
					title: { text: m.dash_axis_per_hour(), style: { color: '#94a3b8', fontSize: '11px' } },
					labels: {
						style: { colors: '#94a3b8', fontSize: '11px' },
						formatter: (val: number) => Math.round(val).toString()
					}
				},
				{
					seriesName: seriesBacklog,
					opposite: true,
					title: { text: m.dash_axis_backlog(), style: { color: '#ef4444', fontSize: '11px' } },
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

		// One-shot guard so the reactive trigger below fires exactly
		// once when auth + tenant-pinned scope land. Without this we
		// hit a race: on first mount ``$authSession.user`` is null
		// (auth/me hasn't resolved yet), which makes
		// ``$isMsspScope`` false, which would trigger an early load
		// for the wrong session. Wait for ``user`` to be known.
		let loaded = false;

		async function loadDashboard() {
			loaded = true;
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
				error = e instanceof Error ? e.message : m.dash_load_metrics_failed();
			} finally {
				loading = false;
			}
		}

		// No onMount here — auth might still be loading. The reactive
		// watcher below fires the load exactly when (a) auth has
		// resolved (``user`` populated) and (b) the session resolves
		// to tenant scope (cross-tenant MSSP scope is owned by
		// MsspDashboard, which renders in the {#if} branch above).
		// This also handles the drill-in flow: same-route goto from
		// MsspAnalytics doesn't remount the page, but the store
		// update flips ``$isMsspScope`` and this watcher fires the
		// per-tenant load.
		$: if ($authSession.user && !$isMsspScope && !loaded) {
			loadDashboard();
		}

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

	// Labels come from the shared EVENT_TYPE_MESSAGES map in formatters.ts (one
	// place to add an event type); only the badge variants are dashboard-local.
	// Message functions are called only when a row renders so the locale is
	// already resolved (never evaluate m.x() at module scope).
	const EVENT_TYPE_VARIANTS: Record<string, string> = {
		'investigation.created': 'variant-soft-primary',
		'investigation.closed': 'variant-soft-success',
		'human.review_requested': 'variant-soft-warning',
		'human.decision_received': 'variant-soft-success',
		'verdict.rendered': 'variant-soft-tertiary',
		'enrichment.failed': 'variant-soft-error',
		'thehive.case_created': 'variant-soft-success',
		'supervisor.decision': 'variant-soft-tertiary'
	};
	function formatEventType(type: string): { label: string; variant: string } {
		const label = EVENT_TYPE_MESSAGES[type];
		if (label) return { label: label(), variant: EVENT_TYPE_VARIANTS[type] ?? 'variant-soft' };
		return { label: type.replace(/[._]/g, ' ').replace(/\b\w/g, c => c.toUpperCase()), variant: 'variant-soft' };
	}

	function formatEventDetails(event: { type: string; data: Record<string, unknown> }): string {
		const d = event.data;
		switch (event.type) {
			case 'investigation.created':
				return d.title ? `"${d.title}"` : m.dash_evd_new_investigation();
			case 'investigation.closed':
				return d.verdict_decision ? formatDecision(d.verdict_decision as string) : m.dash_evd_investigation_closed();
			case 'human.review_requested': {
				if (!d.verdict_decision) return m.dash_evd_awaiting_review();
				const verdict = formatDecision(d.verdict_decision as string);
				return d.verdict_confidence
					? m.dash_evd_suggested_conf({ verdict, pct: Math.round((d.verdict_confidence as number) * 100) })
					: m.dash_evd_suggested({ verdict });
			}
			case 'human.decision_received':
				return d.decision ? m.dash_evd_analyst({ decision: formatDecision(d.decision as string) }) : m.dash_evd_review_submitted();
			case 'verdict.rendered': {
				if (!d.decision) return m.dash_evd_verdict_determined();
				const decision = formatDecision(d.decision as string);
				return d.confidence
					? m.dash_evd_verdict_conf({ decision, pct: Math.round((d.confidence as number) * 100) })
					: decision;
			}
			case 'enrichment.completed':
				return d.analyzer
					? m.dash_evd_analyzer_result({ analyzer: String(d.analyzer), verdict: String(d.verdict || m.dash_evd_done()) })
					: m.dash_evd_enrichment_complete();
			case 'enrichment.requested':
				return d.analyzer ? m.dash_evd_running_analyzer({ analyzer: String(d.analyzer) }) : m.dash_evd_starting_enrichment();
			case 'enrichment.failed':
				return d.analyzer ? m.dash_evd_analyzer_failed({ analyzer: String(d.analyzer) }) : m.dash_evd_enrichment_error();
			case 'thehive.case_created':
				return d.case_number ? m.dash_evd_case_number({ num: String(d.case_number) }) : m.dash_evd_case_opened();
			case 'phase.changed':
				return d.to_phase ? m.dash_evd_phase_now({ phase: formatPhase(d.to_phase as string) }) : m.dash_evd_phase_updated();
			case 'alert.correlated':
				return d.rule_description ? `${d.rule_description}` : m.dash_evd_alert_correlated();
			case 'observable.extracted':
				return d.value ? `${d.type}: ${d.value}` : m.dash_evd_observable_detected();
			case 'supervisor.decision':
				return d.action ? formatAction(d.action as string) : m.dash_evd_decision_recorded();
			case 'misp.context_retrieved':
				return d.event_count ? m.dash_evd_related_events({ count: d.event_count as number }) : m.dash_evd_threat_intel();
			case 'wazuh.forensics_collected':
				return d.agent_id ? m.dash_evd_agent({ id: String(d.agent_id) }) : m.dash_evd_forensics_collected();
			default: {
				// Fallback: show first meaningful field value
				const firstValue = Object.values(d).find(v => typeof v === 'string' && v.length < 60);
				return firstValue ? String(firstValue) : m.dash_evd_event_recorded();
			}
		}
	}
</script>

<svelte:head>
	<title>{m.nav_dashboard()} - SocTalk</title>
</svelte:head>

<!--
  Two dashboards behind one route. ``isMsspScope`` is true only when an
  MSSP user has no current_tenant pin (cross-tenant view). Pinning a
  tenant via "Open SOC" flips it false and falls through to the legacy
  per-tenant dashboard below — same data shape canonical SocTalk has
  always rendered. Customer roles never reach MSSP scope.
-->
{#if $isMsspScope}
	<MsspDashboard />
{:else}
<div class="flex items-center justify-between mb-6">
	<h1 class="h2">{m.nav_dashboard()}</h1>
	<button class="btn variant-soft" on:click={loadDashboard} disabled={loading}>
		{#if loading}
			<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
		{/if}
		{m.dash_refresh()}
	</button>
</div>

{#if loading}
	<div class="flex items-center justify-center h-64">
		<div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500"></div>
	</div>
{:else if error}
	<div class="alert variant-filled-error">
		<span>{m.dash_error({ error })}</span>
	</div>
{:else if metrics}
	<!-- Fleet flight recorder, live head (#72): present tense on the server
	     clock — quiet is legible state, not an animation ritual. -->
	<FleetPanel defaultMode="live" />

	<!-- KPI Cards -->
	<div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
		<div class="card p-4 variant-soft">
			<h3 class="text-sm opacity-60">{m.dash_kpi_open_investigations()}</h3>
			<p class="text-3xl font-bold">{metrics.open_investigations}</p>
		</div>
		<div class="card p-4 variant-soft-warning">
			<h3 class="text-sm opacity-60">{m.dash_kpi_pending_reviews()}</h3>
			<p class="text-3xl font-bold">{metrics.pending_reviews}</p>
		</div>
		<div class="card p-4 variant-soft">
			<h3 class="text-sm opacity-60">{m.dash_kpi_avg_time_to_triage()}</h3>
			<p class="text-3xl font-bold">{formatDuration(metrics.avg_time_to_triage_seconds)}</p>
		</div>
		<div class="card p-4 variant-soft">
			<h3 class="text-sm opacity-60">{m.dash_kpi_avg_time_to_verdict()}</h3>
			<p class="text-3xl font-bold">{formatDuration(metrics.avg_time_to_verdict_seconds)}</p>
		</div>
	</div>

	<!-- Today's Activity -->
	<div class="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-6">
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.dash_kpi_created_today()}</h4>
			<p class="text-xl font-bold">{metrics.investigations_created_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.dash_kpi_closed_today()}</h4>
			<p class="text-xl font-bold">{metrics.investigations_closed_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.dash_kpi_escalations()}</h4>
			<p class="text-xl font-bold">{metrics.escalations_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.dash_kpi_auto_closed()}</h4>
			<p class="text-xl font-bold">{metrics.auto_closed_today}</p>
		</div>
		<div class="card p-3">
			<h4 class="text-xs opacity-60">{m.dash_kpi_malicious_iocs()}</h4>
			<p class="text-xl font-bold text-error-500">{metrics.malicious_observables_today}</p>
		</div>
	</div>

	<!-- Charts Row -->
	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
		<!-- Investigation Throughput Chart -->
		<div class="card p-4 lg:col-span-2">
			<h3 class="h4 mb-4">{m.dash_throughput_24h()}</h3>
			<div bind:this={chartElement} class="h-64"></div>
			{#if hourlyData && hourlyData.metrics.length === 0}
				<p class="opacity-60 text-center py-8">{m.dash_no_hourly_data()}</p>
			{/if}
		</div>

		<!-- Verdict Distribution -->
		<div class="card p-4">
			<h3 class="h4 mb-4">{m.dash_verdicts_today()}</h3>
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
						<span class="w-2 h-2 rounded bg-error-500"></span> {m.dec_escalate()}
					</span>
					<span class="flex items-center gap-1">
						<span class="w-2 h-2 rounded bg-warning-500"></span> {m.dash_legend_needs_more_info()}
					</span>
					<span class="flex items-center gap-1">
						<span class="w-2 h-2 rounded bg-success-500"></span> {m.dec_close()}
					</span>
				</div>
			{:else}
				<p class="opacity-60 text-center py-8">{m.dash_no_verdicts_today()}</p>
			{/if}
		</div>
	</div>

	<!-- Active Investigations Status Board -->
	<div class="card p-4 mb-6">
		<h3 class="h4 mb-4">{m.dash_active_investigations()}</h3>
		{#if activeInvestigations.length > 0}
			<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
				{#each activeInvestigations as inv}
					<a href={localizeHref(`/investigations/${inv.id}`)} class="card p-3 variant-soft hover:variant-soft-primary transition-colors">
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
						<h4 class="font-medium text-sm truncate">{inv.title || m.dash_untitled_investigation()}</h4>
						<div class="flex items-center gap-3 mt-2 text-xs opacity-60">
							<span>{m.dash_phase_label({ phase: formatPhase(inv.phase) })}</span>
							<span>{m.dash_alerts_count({ count: inv.alert_count })}</span>
							{#if inv.malicious_count > 0}
								<span class="text-error-500">{m.dash_malicious_count({ count: inv.malicious_count })}</span>
							{/if}
						</div>
					</a>
				{/each}
			</div>
		{:else}
			<p class="opacity-60 text-center py-8">{m.dash_no_active_investigations()}</p>
		{/if}
	</div>

	<!-- Bottom Row: Recent Investigations + Severity Breakdown -->
	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
		<!-- Recent Investigations Table -->
		<div class="card p-4 lg:col-span-2">
			<h3 class="h4 mb-4">{m.dash_recent_investigations()}</h3>
			{#if recentInvestigations.length > 0}
				<div class="table-container">
					<table class="table table-compact">
						<thead>
							<tr>
								<th>{m.dash_th_title()}</th>
								<th>{m.dash_th_status()}</th>
								<th>{m.dash_th_verdict()}</th>
								<th>{m.dash_th_alerts()}</th>
								<th>{m.dash_th_created()}</th>
							</tr>
						</thead>
						<tbody>
							{#each recentInvestigations.slice(0, 5) as inv}
								<tr>
									<td class="max-w-xs truncate">
										<a href={localizeHref(`/investigations/${inv.id}`)} class="anchor">
											{inv.title || m.dash_untitled()}
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
					<a href={localizeHref('/investigations')} class="anchor text-sm">{m.dash_view_all_investigations()}</a>
				</div>
			{:else}
				<p class="opacity-60 text-center py-8">{m.dash_no_investigations_yet()}</p>
			{/if}
		</div>

		<!-- Severity Breakdown -->
		<div class="card p-4">
			<h3 class="h4 mb-4">{m.dash_open_by_severity()}</h3>
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
					<p class="opacity-60 text-center py-4">{m.dash_no_open_investigations()}</p>
				{/if}
			</div>
		</div>
	</div>

	<!-- Recent Events -->
	<div class="card p-4">
		<h3 class="h4 mb-4">{m.dash_live_event_stream()}</h3>
		{#if $recentEvents.length > 0}
			<div class="table-container">
				<table class="table table-compact">
					<thead>
						<tr>
							<th>{m.dash_th_time()}</th>
							<th>{m.dash_th_event()}</th>
							<th>{m.dash_th_details()}</th>
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
			<p class="opacity-60 text-center py-8">{m.dash_no_events_yet()}</p>
		{/if}
	</div>
{/if}
{/if}
