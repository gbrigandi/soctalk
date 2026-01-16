	<script lang="ts">
		import { onMount } from 'svelte';
		import { api, type InvestigationSummary } from '$lib/api/client';
		import { formatStatus, formatPhase, formatSeverity, formatDecision } from '$lib/utils/formatters';

		let investigations: InvestigationSummary[] = [];
		let loading = true;
		let error: string | null = null;
		let page = 1;
		let total = 0;
	let hasMore = false;

	// Filters
	let statusFilter = '';
	let phaseFilter = '';

	onMount(() => loadInvestigations());

	async function loadInvestigations() {
		loading = true;
		error = null;
		try {
			const result = await api.investigations.list({
				page,
				page_size: 20,
				status: statusFilter || undefined,
				phase: phaseFilter || undefined
			});
			investigations = result.items;
			total = result.total;
			hasMore = result.has_more;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load investigations';
		} finally {
			loading = false;
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
</script>

<svelte:head>
	<title>Investigations - SocTalk</title>
</svelte:head>

<div class="flex items-center justify-between mb-4">
	<h1 class="h2">Investigations</h1>
	<button class="btn variant-soft" on:click={loadInvestigations} disabled={loading}>
		{#if loading}
			<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
		{/if}
		Refresh
	</button>
</div>

<!-- Filters -->
<div class="flex flex-wrap gap-4 mb-4">
	<select class="select" bind:value={statusFilter} on:change={() => { page = 1; loadInvestigations(); }}>
		<option value="">All Statuses</option>
		<option value="pending">Pending</option>
		<option value="in_progress">In Progress</option>
		<option value="paused">Paused</option>
		<option value="escalated">Escalated</option>
		<option value="auto_closed">Auto-Closed</option>
		<option value="closed">Closed</option>
		<option value="rejected">Rejected</option>
		<option value="cancelled">Cancelled</option>
	</select>
	<select class="select" bind:value={phaseFilter} on:change={() => { page = 1; loadInvestigations(); }}>
		<option value="">All Phases</option>
		<option value="triage">Triage</option>
		<option value="enrichment">Enrichment</option>
		<option value="analysis">Analysis</option>
		<option value="verdict">Verdict</option>
		<option value="human_review">Human Review</option>
		<option value="closed">Closed</option>
	</select>
</div>

{#if loading}
	<div class="flex items-center justify-center h-64">
		<div class="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500"></div>
	</div>
{:else if error}
	<div class="alert variant-filled-error">
		<span>Error: {error}</span>
	</div>
{:else}
	<div class="table-container">
		<table class="table table-hover">
			<thead>
				<tr>
					<th>Title</th>
					<th>Status</th>
					<th>Phase</th>
					<th>Severity</th>
					<th>Alerts</th>
					<th>Malicious</th>
					<th>Verdict</th>
					<th>Created</th>
					<th>Actions</th>
				</tr>
			</thead>
			<tbody>
				{#each investigations as inv}
					<tr>
						<td class="max-w-xs truncate">
							<a href="/investigations/{inv.id}" class="anchor">
								{inv.title || 'Untitled Investigation'}
							</a>
						</td>
						<td><span class="badge {getStatusBadge(inv.status)}">{formatStatus(inv.status)}</span></td>
						<td><span class="badge variant-soft">{formatPhase(inv.phase)}</span></td>
						<td>
							{#if inv.max_severity}
								<span class="badge {getSeverityBadge(inv.max_severity)}">{formatSeverity(inv.max_severity)}</span>
							{:else}
								<span class="opacity-40">-</span>
							{/if}
						</td>
						<td>{inv.alert_count}</td>
						<td class="text-error-500">{inv.malicious_count}</td>
						<td>
							{#if inv.verdict_decision}
								<span class="badge {inv.verdict_decision === 'escalate' ? 'variant-filled-error' :
								                    inv.verdict_decision === 'needs_more_info' || inv.verdict_decision === 'suspicious' ? 'variant-filled-warning' :
								                    inv.verdict_decision === 'close' || inv.verdict_decision === 'auto_close' ? 'variant-filled-success' :
								                    'variant-soft'}">
									{formatDecision(inv.verdict_decision)}
								</span>
							{:else}
								<span class="opacity-40">-</span>
							{/if}
						</td>
						<td class="text-xs opacity-60">
							{new Date(inv.created_at).toLocaleString()}
						</td>
						<td>
							<a href="/investigations/{inv.id}" class="btn btn-sm variant-soft">View</a>
						</td>
					</tr>
				{/each}
				{#if investigations.length === 0}
					<tr>
						<td colspan="9" class="text-center opacity-60 py-8">
							No investigations found
						</td>
					</tr>
				{/if}
			</tbody>
		</table>
	</div>

	<!-- Pagination -->
	{#if total > 20}
		<div class="flex justify-between items-center mt-4">
			<span class="text-sm opacity-60">
				Showing {(page - 1) * 20 + 1} - {Math.min(page * 20, total)} of {total}
			</span>
			<div class="flex gap-2">
				<button
					class="btn btn-sm variant-soft"
					disabled={page <= 1}
					on:click={() => { page--; loadInvestigations(); }}
				>
					Previous
				</button>
				<button
					class="btn btn-sm variant-soft"
					disabled={!hasMore}
					on:click={() => { page++; loadInvestigations(); }}
				>
					Next
				</button>
			</div>
		</div>
	{/if}
{/if}
