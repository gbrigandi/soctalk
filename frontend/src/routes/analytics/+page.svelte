<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { browser } from '$app/environment';
	import { api, type AnalyticsSummary } from '$lib/api/client';
	import { formatDecision, formatSeverity, formatDuration, formatPercent } from '$lib/utils/formatters';

	let analytics: AnalyticsSummary | null = null;
	let loading = true;
	let error: string | null = null;
	let selectedDays = 7;

	// Chart instances
	let confidenceChartEl: HTMLDivElement;
	let decisionTrendChartEl: HTMLDivElement;
	let reviewPieChartEl: HTMLDivElement;
	let outcomeChartEl: HTMLDivElement;

	let confidenceChart: any = null;
	let decisionTrendChart: any = null;
	let reviewPieChart: any = null;
	let outcomeChart: any = null;

	async function loadAnalytics() {
		loading = true;
		error = null;
		try {
			analytics = await api.analytics.summary(selectedDays);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load analytics';
		} finally {
			loading = false;
		}
	}

	async function initCharts() {
		if (!browser || !analytics) return;

		const ApexCharts = (await import('apexcharts')).default;

		// Confidence Distribution Chart (Bar)
		if (confidenceChartEl) {
			const confOptions = {
				chart: {
					type: 'bar',
					height: 200,
					background: 'transparent',
					toolbar: { show: false },
					fontFamily: 'inherit'
				},
				series: [{
					name: 'Investigations',
					data: analytics.ai_behavior.confidence_distribution.map(b => b.count)
				}],
				colors: ['#6366f1'],
				plotOptions: {
					bar: {
						borderRadius: 4,
						horizontal: false,
						columnWidth: '60%',
					}
				},
				dataLabels: { enabled: false },
				xaxis: {
					categories: analytics.ai_behavior.confidence_distribution.map(b => b.range_label),
					labels: { style: { colors: '#94a3b8', fontSize: '11px' } },
					axisBorder: { show: false },
					axisTicks: { show: false }
				},
				yaxis: {
					labels: { style: { colors: '#94a3b8', fontSize: '11px' } }
				},
				grid: {
					borderColor: '#334155',
					strokeDashArray: 4,
				},
				tooltip: { theme: 'dark' }
			};

			if (confidenceChart) confidenceChart.destroy();
			confidenceChart = new ApexCharts(confidenceChartEl, confOptions);
			confidenceChart.render();
		}

		// Decision Trends Chart (Stacked Area)
		if (decisionTrendChartEl && analytics.ai_behavior.decision_trends.length > 0) {
			const trendOptions = {
				chart: {
					type: 'area',
					height: 200,
					stacked: true,
					background: 'transparent',
					toolbar: { show: false },
					fontFamily: 'inherit'
				},
				series: [
					{ name: 'Close', data: analytics.ai_behavior.decision_trends.map(t => t.close) },
					{ name: 'Escalate', data: analytics.ai_behavior.decision_trends.map(t => t.escalate) },
					{ name: 'Needs Info', data: analytics.ai_behavior.decision_trends.map(t => t.needs_more_info) },
					{ name: 'Suspicious', data: analytics.ai_behavior.decision_trends.map(t => t.suspicious) },
				],
				colors: ['#22c55e', '#ef4444', '#f59e0b', '#8b5cf6'],
				fill: {
					type: 'gradient',
					gradient: { opacityFrom: 0.6, opacityTo: 0.1 }
				},
				stroke: { curve: 'smooth', width: 2 },
				dataLabels: { enabled: false },
				xaxis: {
					categories: analytics.ai_behavior.decision_trends.map(t => {
						const d = new Date(t.period);
						return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
					}),
					labels: { style: { colors: '#94a3b8', fontSize: '11px' } },
					axisBorder: { show: false },
					axisTicks: { show: false }
				},
				yaxis: {
					labels: { style: { colors: '#94a3b8', fontSize: '11px' } }
				},
				grid: { borderColor: '#334155', strokeDashArray: 4 },
				legend: {
					position: 'top',
					horizontalAlign: 'right',
					labels: { colors: '#94a3b8' }
				},
				tooltip: { theme: 'dark' }
			};

			if (decisionTrendChart) decisionTrendChart.destroy();
			decisionTrendChart = new ApexCharts(decisionTrendChartEl, trendOptions);
			decisionTrendChart.render();
		}

		// Human Review Pie Chart
		if (reviewPieChartEl && analytics.human_review.total_reviews > 0) {
			const pieOptions = {
				chart: {
					type: 'donut',
					height: 200,
					background: 'transparent',
					fontFamily: 'inherit'
				},
				series: [
					analytics.human_review.approved,
					analytics.human_review.rejected,
					analytics.human_review.info_requested,
					analytics.human_review.expired,
				],
				labels: ['Approved', 'Rejected', 'More Info', 'Expired'],
				colors: ['#22c55e', '#ef4444', '#f59e0b', '#64748b'],
				stroke: { show: false },
				dataLabels: { enabled: false },
				legend: {
					position: 'bottom',
					labels: { colors: '#94a3b8' }
				},
				plotOptions: {
					pie: {
						donut: {
							size: '70%',
							labels: {
								show: true,
								total: {
									show: true,
									label: 'Total',
									color: '#94a3b8',
									formatter: () => analytics?.human_review.total_reviews.toString() || '0'
								}
							}
						}
					}
				},
				tooltip: { theme: 'dark' }
			};

			if (reviewPieChart) reviewPieChart.destroy();
			reviewPieChart = new ApexCharts(reviewPieChartEl, pieOptions);
			reviewPieChart.render();
		}

		// Outcomes Chart (Horizontal Bar)
		if (outcomeChartEl) {
			const outcomeOptions = {
				chart: {
					type: 'bar',
					height: 150,
					background: 'transparent',
					toolbar: { show: false },
					fontFamily: 'inherit'
				},
				series: [{
					name: 'Count',
					data: [
						analytics.outcomes.closed_as_false_positive,
						analytics.outcomes.closed_as_true_positive,
						analytics.outcomes.closed_as_suspicious,
					]
				}],
				colors: ['#22c55e', '#ef4444', '#f59e0b'],
				plotOptions: {
					bar: {
						horizontal: true,
						borderRadius: 4,
						distributed: true,
						barHeight: '70%',
					}
				},
				dataLabels: {
					enabled: true,
					style: { colors: ['#fff'], fontSize: '12px' }
				},
				xaxis: {
					categories: ['False Positive', 'True Positive', 'Suspicious'],
					labels: { style: { colors: '#94a3b8', fontSize: '11px' } },
				},
				yaxis: {
					labels: { style: { colors: '#94a3b8', fontSize: '11px' } }
				},
				grid: { borderColor: '#334155', strokeDashArray: 4 },
				legend: { show: false },
				tooltip: { theme: 'dark' }
			};

			if (outcomeChart) outcomeChart.destroy();
			outcomeChart = new ApexCharts(outcomeChartEl, outcomeOptions);
			outcomeChart.render();
		}
	}

	$: if (analytics && browser) {
		initCharts();
	}

	onMount(() => {
		loadAnalytics();
	});

	onDestroy(() => {
		if (confidenceChart) confidenceChart.destroy();
		if (decisionTrendChart) decisionTrendChart.destroy();
		if (reviewPieChart) reviewPieChart.destroy();
		if (outcomeChart) outcomeChart.destroy();
	});
</script>

<svelte:head>
	<title>Analytics - SocTalk</title>
</svelte:head>

<!-- Header with Period Selector -->
<div class="flex items-center justify-between mb-6">
	<h1 class="h2">AI Analytics</h1>
	<div class="flex items-center gap-2">
		<span class="text-sm opacity-60">Period:</span>
		<select
			class="select w-32"
			bind:value={selectedDays}
			on:change={() => loadAnalytics()}
		>
			<option value={1}>Last 24h</option>
			<option value={7}>Last 7 days</option>
			<option value={30}>Last 30 days</option>
			<option value={90}>Last 90 days</option>
		</select>
	</div>
</div>

{#if loading}
	<div class="flex items-center justify-center h-64">
		<div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500"></div>
	</div>
{:else if error}
	<div class="alert variant-filled-error">
		<span>Error: {error}</span>
	</div>
{:else if analytics}
	<!-- Executive KPIs -->
	<section class="mb-8">
		<h2 class="h4 mb-4 opacity-60">Executive KPIs</h2>
		<div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
			<!-- Auto-Close Rate -->
			<div class="card p-4">
				<div class="text-sm opacity-60 mb-1">Auto-Close Rate</div>
				<div class="text-3xl font-bold text-success-500">
					{formatPercent(analytics.executive_kpis.auto_close_rate)}
				</div>
				<div class="text-xs opacity-40 mt-1">
					{analytics.executive_kpis.auto_closed_count} / {analytics.executive_kpis.total_investigations} investigations
				</div>
			</div>

			<!-- Escalation Rate -->
			<div class="card p-4">
				<div class="text-sm opacity-60 mb-1">Escalation Rate</div>
				<div class="text-3xl font-bold text-error-500">
					{formatPercent(analytics.executive_kpis.escalation_rate)}
				</div>
				<div class="text-xs opacity-40 mt-1">
					{analytics.executive_kpis.escalated_count} escalated to incidents
				</div>
			</div>

			<!-- Human Override Rate -->
			<div class="card p-4">
				<div class="text-sm opacity-60 mb-1">Human Override Rate</div>
				<div class="text-3xl font-bold text-warning-500">
					{formatPercent(analytics.executive_kpis.human_override_rate)}
				</div>
				<div class="text-xs opacity-40 mt-1">
					AI decisions changed by analysts
				</div>
			</div>

			<!-- Mean Time to Decision -->
			<div class="card p-4">
				<div class="text-sm opacity-60 mb-1">Mean Time to Decision</div>
				<div class="text-3xl font-bold text-primary-500">
					{formatDuration(analytics.executive_kpis.mean_time_to_decision_seconds)}
				</div>
				<div class="text-xs opacity-40 mt-1">
					Avg AI confidence: {analytics.executive_kpis.avg_ai_confidence ? formatPercent(analytics.executive_kpis.avg_ai_confidence) : '-'}
				</div>
			</div>
		</div>
	</section>

	<!-- AI Behavior Section -->
	<section class="mb-8">
		<h2 class="h4 mb-4 opacity-60">AI Behavior</h2>
		<div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
			<!-- Confidence Distribution -->
			<div class="card p-4">
				<h3 class="text-sm font-semibold mb-2">Confidence Distribution</h3>
				<div bind:this={confidenceChartEl} class="h-48"></div>
				<div class="text-xs opacity-40 mt-2 text-center">
					High confidence (>80%): {formatPercent(analytics.executive_kpis.high_confidence_rate)}
				</div>
			</div>

			<!-- Decision Trends -->
			<div class="card p-4">
				<h3 class="text-sm font-semibold mb-2">Decision Trends</h3>
				{#if analytics.ai_behavior.decision_trends.length > 0}
					<div bind:this={decisionTrendChartEl} class="h-48"></div>
				{:else}
					<div class="h-48 flex items-center justify-center opacity-40">
						No trend data available
					</div>
				{/if}
			</div>
		</div>

		<!-- Confidence by Decision Type -->
		{#if Object.keys(analytics.ai_behavior.avg_confidence_by_decision).length > 0}
			<div class="card p-4 mt-4">
				<h3 class="text-sm font-semibold mb-3">Average Confidence by Decision</h3>
				<div class="flex flex-wrap gap-4">
					{#each Object.entries(analytics.ai_behavior.avg_confidence_by_decision) as [decision, confidence]}
						<div class="flex items-center gap-2">
							<span class="badge {decision === 'escalate' ? 'variant-filled-error' : decision === 'close' || decision === 'auto_close' ? 'variant-filled-success' : 'variant-filled-warning'}">
								{formatDecision(decision)}
							</span>
							<span class="font-mono text-sm">{formatPercent(confidence)}</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Escalation Breakdown by Severity -->
		{#if analytics.ai_behavior.escalation_breakdown.length > 0}
			<div class="card p-4 mt-4">
				<h3 class="text-sm font-semibold mb-3">Escalation Breakdown by Severity</h3>
				<div class="space-y-2">
					{#each analytics.ai_behavior.escalation_breakdown as item}
						<div class="flex items-center gap-3">
							<div class="w-32 text-sm">{item.reason}</div>
							<div class="flex-1 h-6 bg-surface-700 rounded-full overflow-hidden">
								<div
									class="h-full {item.reason.includes('Critical') ? 'bg-error-500' : item.reason.includes('High') ? 'bg-warning-500' : item.reason.includes('Medium') ? 'bg-secondary-500' : 'bg-surface-500'}"
									style="width: {item.percentage * 100}%"
								></div>
							</div>
							<div class="w-16 text-right text-sm font-mono">
								{item.count} <span class="opacity-40">({formatPercent(item.percentage)})</span>
							</div>
						</div>
					{/each}
				</div>
			</div>
		{/if}
	</section>

	<!-- Human-in-the-Loop Section -->
	<section class="mb-8">
		<h2 class="h4 mb-4 opacity-60">Human-in-the-Loop</h2>
		<div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
			<!-- Review Stats Cards -->
			<div class="card p-4">
				<h3 class="text-sm font-semibold mb-3">Review Volume</h3>
				<div class="space-y-3">
					<div class="flex justify-between items-center">
						<span class="opacity-60">Total Reviews</span>
						<span class="font-bold">{analytics.human_review.total_reviews}</span>
					</div>
					<div class="flex justify-between items-center">
						<span class="opacity-60">Currently Pending</span>
						<span class="font-bold text-warning-500">{analytics.human_review.pending}</span>
					</div>
					<div class="flex justify-between items-center">
						<span class="opacity-60">Avg Review Time</span>
						<span class="font-bold">{formatDuration(analytics.human_review.avg_review_time_seconds)}</span>
					</div>
				</div>
			</div>

			<!-- Review Outcomes Pie -->
			<div class="card p-4">
				<h3 class="text-sm font-semibold mb-2">Review Outcomes</h3>
				{#if analytics.human_review.total_reviews > 0}
					<div bind:this={reviewPieChartEl} class="h-48"></div>
				{:else}
					<div class="h-48 flex items-center justify-center opacity-40">
						No review data available
					</div>
				{/if}
			</div>

			<!-- Agreement/Override Stats -->
			<div class="card p-4">
				<h3 class="text-sm font-semibold mb-3">AI Agreement</h3>
				<div class="space-y-3">
					<div class="flex justify-between items-center">
						<span class="opacity-60">AI Agreed (Approved)</span>
						<span class="font-bold text-success-500">{analytics.human_review.ai_agreed_count}</span>
					</div>
					<div class="flex justify-between items-center">
						<span class="opacity-60">AI Overridden</span>
						<span class="font-bold text-error-500">{analytics.human_review.ai_overridden_count}</span>
					</div>
					<hr class="opacity-20" />
					<div class="flex justify-between items-center">
						<span class="opacity-60">Approval Rate</span>
						<span class="font-bold">{formatPercent(analytics.human_review.approval_rate)}</span>
					</div>
					<div class="flex justify-between items-center">
						<span class="opacity-60">Override Rate</span>
						<span class="font-bold text-warning-500">{formatPercent(analytics.human_review.override_rate)}</span>
					</div>
				</div>
			</div>
		</div>
	</section>

	<!-- Outcomes Section -->
	<section class="mb-8">
		<h2 class="h4 mb-4 opacity-60">Investigation Outcomes</h2>
		<div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
			<!-- Resolution Times -->
			<div class="card p-4">
				<h3 class="text-sm font-semibold mb-3">Resolution Times</h3>
				<div class="grid grid-cols-3 gap-4 text-center">
					<div>
						<div class="text-2xl font-bold text-primary-500">
							{formatDuration(analytics.outcomes.avg_resolution_time_seconds)}
						</div>
						<div class="text-xs opacity-40">Average</div>
					</div>
					<div>
						<div class="text-2xl font-bold">
							{formatDuration(analytics.outcomes.p50_resolution_time_seconds)}
						</div>
						<div class="text-xs opacity-40">Median (p50)</div>
					</div>
					<div>
						<div class="text-2xl font-bold text-warning-500">
							{formatDuration(analytics.outcomes.p90_resolution_time_seconds)}
						</div>
						<div class="text-xs opacity-40">p90</div>
					</div>
				</div>
				<div class="mt-4 text-center text-sm opacity-60">
					{analytics.outcomes.total_closed} investigations closed
				</div>
			</div>

			<!-- Outcome Breakdown -->
			<div class="card p-4">
				<h3 class="text-sm font-semibold mb-2">Verdict Breakdown</h3>
				<div bind:this={outcomeChartEl} class="h-36"></div>
			</div>
		</div>
	</section>
{/if}
