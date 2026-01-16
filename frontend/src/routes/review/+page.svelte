<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type PendingReview, ApiError } from '$lib/api/client';
	import { addToast, pendingReviewsCount } from '$lib/stores';
	import { formatSeverity, formatStatus, formatDecision } from '$lib/utils/formatters';

	let reviews: PendingReview[] = [];
	let loading = true;
	let error: string | null = null;
	let selectedReview: PendingReview | null = null;
	let expandedReviews: Set<string> = new Set();
	let feedback = '';
	let processing = false;
	let showRequestInfoModal = false;
	let requestInfoQuestions: string[] = [''];

	function toggleExpand(reviewId: string) {
		if (expandedReviews.has(reviewId)) {
			expandedReviews.delete(reviewId);
		} else {
			expandedReviews.add(reviewId);
		}
		expandedReviews = new Set(expandedReviews); // create new Set to trigger reactivity
	}

	onMount(() => loadReviews());

	function handleReviewError(e: unknown, action: string): void {
		// Check for race condition (409 Conflict)
		if (e instanceof ApiError && e.status === 409) {
			addToast({
				type: 'info',
				title: 'Already Handled',
				message: 'This review was already handled via Slack. Refreshing list...'
			});
			selectedReview = null;
			feedback = '';
			loadReviews();
			return;
		}
		// Generic error
		addToast({
			type: 'error',
			title: 'Error',
			message: e instanceof Error ? e.message : `Failed to ${action}`
		});
	}

	async function loadReviews() {
		loading = true;
		error = null;
		try {
			const result = await api.review.listPending();
			reviews = result.items;
			pendingReviewsCount.set(result.total);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load reviews';
		} finally {
			loading = false;
		}
	}

	async function approveReview(id: string) {
		processing = true;
		try {
			await api.review.approve(id, feedback || undefined);
			addToast({ type: 'success', title: 'Approved', message: 'Review approved successfully' });
			selectedReview = null;
			feedback = '';
			await loadReviews();
		} catch (e) {
			handleReviewError(e, 'approve');
		} finally {
			processing = false;
		}
	}

	async function rejectReview(id: string) {
		if (!feedback.trim()) {
			addToast({ type: 'warning', title: 'Required', message: 'Please provide feedback for rejection' });
			return;
		}
		processing = true;
		try {
			await api.review.reject(id, feedback);
			addToast({ type: 'success', title: 'Rejected', message: 'Review rejected' });
			selectedReview = null;
			feedback = '';
			await loadReviews();
		} catch (e) {
			handleReviewError(e, 'reject');
		} finally {
			processing = false;
		}
	}

	async function requestMoreInfo(id: string) {
		const questions = requestInfoQuestions.filter(q => q.trim());
		if (questions.length === 0) {
			addToast({ type: 'warning', title: 'Required', message: 'Please add at least one question' });
			return;
		}
		processing = true;
		try {
			await api.review.requestInfo(id, questions);
			addToast({ type: 'success', title: 'Request Sent', message: 'Additional information requested' });
			showRequestInfoModal = false;
			requestInfoQuestions = [''];
			selectedReview = null;
			await loadReviews();
		} catch (e) {
			handleReviewError(e, 'request info');
			showRequestInfoModal = false;
		} finally {
			processing = false;
		}
	}

	function addQuestion() {
		requestInfoQuestions = [...requestInfoQuestions, ''];
	}

	function removeQuestion(index: number) {
		requestInfoQuestions = requestInfoQuestions.filter((_, i) => i !== index);
	}

	function getSeverityColor(severity: string): string {
		switch (severity?.toLowerCase()) {
			case 'critical': return 'variant-filled-error';
			case 'high': return 'variant-filled-warning';
			case 'medium': return 'variant-filled-secondary';
			default: return 'variant-soft';
		}
	}

	function getDecisionColor(decision: string | null): string {
		switch (decision) {
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

	function formatTimeRemaining(expiresAt: string | null): string {
		if (!expiresAt) return 'No deadline';
		const expires = new Date(expiresAt);
		const now = new Date();
		const diff = expires.getTime() - now.getTime();
		if (diff <= 0) return 'Expired';
		const minutes = Math.floor(diff / 60000);
		const hours = Math.floor(minutes / 60);
		if (hours > 0) return `${hours}h ${minutes % 60}m remaining`;
		return `${minutes}m remaining`;
	}

	function isExpiringSoon(expiresAt: string | null): boolean {
		if (!expiresAt) return false;
		const expires = new Date(expiresAt);
		const now = new Date();
		const diff = expires.getTime() - now.getTime();
		return diff > 0 && diff < 15 * 60 * 1000; // Less than 15 minutes
	}

	function hasEnrichments(review: PendingReview): boolean {
		return review.enrichments && Object.keys(review.enrichments).length > 0;
	}

	function hasMispContext(review: PendingReview): boolean {
		return review.misp_context !== null && Object.keys(review.misp_context).length > 0;
	}
</script>

<svelte:head>
	<title>Human Review - SocTalk</title>
</svelte:head>

	<div class="flex items-center justify-between mb-6">
		<h1 class="h2">Human Review Queue</h1>
		<button class="btn variant-soft" on:click={loadReviews} disabled={loading}>
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
{:else if reviews.length === 0}
	<div class="card p-8 text-center">
		<svg xmlns="http://www.w3.org/2000/svg" class="h-16 w-16 mx-auto opacity-40 mb-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
		</svg>
		<h2 class="h3 mb-2">All Caught Up!</h2>
		<p class="opacity-60">No pending reviews at this time.</p>
	</div>
{:else}
	<div class="grid gap-3">
		{#each reviews as review}
			{@const expanded = expandedReviews.has(review.id) || selectedReview?.id === review.id}
			<div class="card {selectedReview?.id === review.id ? 'ring-2 ring-primary-500' : ''}">
				<!-- Collapsed Header (always visible) -->
				<button
					class="w-full p-4 text-left hover:bg-surface-500/5 transition-colors"
					on:click={() => toggleExpand(review.id)}
				>
					<div class="flex flex-col lg:flex-row lg:items-center gap-3">
						<!-- Expand/Collapse Icon -->
						<svg
							xmlns="http://www.w3.org/2000/svg"
							class="h-5 w-5 opacity-60 transition-transform flex-shrink-0 {expanded ? 'rotate-90' : ''}"
							fill="none"
							viewBox="0 0 24 24"
							stroke="currentColor"
						>
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
						</svg>

						<!-- Title and Quick Info -->
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2 flex-wrap">
								<h3 class="font-semibold truncate">{review.title}</h3>
								{#if isExpiringSoon(review.expires_at)}
									<span class="badge variant-filled-warning animate-pulse text-xs">Urgent</span>
								{/if}
							</div>
							<div class="flex items-center gap-3 text-xs opacity-60 mt-1">
								<span>{review.alert_count} alerts</span>
								{#if review.malicious_count > 0}
									<span class="text-error-500">{review.malicious_count} malicious</span>
								{/if}
								<span>{formatTimeRemaining(review.expires_at)}</span>
							</div>
						</div>

						<!-- Badges -->
						<div class="flex items-center gap-2 flex-shrink-0">
							<span class="badge {getSeverityColor(review.max_severity)} text-xs">
								{formatSeverity(review.max_severity)}
							</span>
							{#if review.ai_decision}
								<span class="badge {getDecisionColor(review.ai_decision)} text-xs">
									AI: {formatDecision(review.ai_decision)}
								</span>
							{/if}
						</div>

						<!-- Quick Action Button (visible when collapsed) -->
						{#if !expanded}
							<button
								class="btn btn-sm variant-filled-primary flex-shrink-0"
								on:click|stopPropagation={() => { expandedReviews.add(review.id); expandedReviews = new Set(expandedReviews); selectedReview = review; }}
							>
								Review
							</button>
						{/if}
					</div>
				</button>

				<!-- Expanded Content -->
				{#if expanded}
					<div class="px-4 pb-4 border-t border-surface-500/10">
						<!-- Investigation Link -->
						<div class="flex items-center gap-2 text-sm py-3">
							<a
								href="/investigations/{review.investigation_id}"
								class="text-primary-500 hover:underline flex items-center gap-1"
								on:click|stopPropagation
							>
								<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
								</svg>
								View Full Investigation
							</a>
						</div>

						<!-- Description -->
						<p class="mb-4 text-sm">{review.description}</p>

						<!-- Stats Grid -->
						<div class="grid grid-cols-4 gap-3 mb-4 text-center">
							<div class="card p-2 variant-soft">
								<div class="text-lg font-bold">{review.alert_count}</div>
								<div class="text-xs opacity-60">Alerts</div>
							</div>
							<div class="card p-2 variant-soft-error">
								<div class="text-lg font-bold text-error-500">{review.malicious_count}</div>
								<div class="text-xs opacity-60">Malicious</div>
							</div>
							<div class="card p-2 variant-soft-warning">
								<div class="text-lg font-bold text-warning-500">{review.suspicious_count}</div>
								<div class="text-xs opacity-60">Suspicious</div>
							</div>
							<div class="card p-2 variant-soft-success">
								<div class="text-lg font-bold text-success-500">{review.clean_count}</div>
								<div class="text-xs opacity-60">Clean</div>
							</div>
						</div>

						<!-- AI Recommendation -->
						{#if review.ai_decision}
							<div class="card p-3 variant-soft-primary mb-4">
								<div class="flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2 mb-2">
									<span class="font-bold flex items-center gap-2 text-sm">
										<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
										</svg>
										AI Recommendation
									</span>
									<span class="badge {getDecisionColor(review.ai_decision)}">
										{formatDecision(review.ai_decision)} ({Math.round((review.ai_confidence || 0) * 100)}%)
									</span>
								</div>
								{#if review.ai_assessment}
									<p class="text-sm mb-2">{review.ai_assessment}</p>
								{/if}
								{#if review.ai_recommendation}
									<p class="text-sm italic opacity-80 bg-surface-500/10 rounded p-2">
										{review.ai_recommendation}
									</p>
								{/if}
							</div>
						{/if}

						<!-- Key Findings -->
						{#if review.findings.length > 0}
							<div class="mb-4">
								<h4 class="font-bold text-sm mb-2 flex items-center gap-2">
									<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
									</svg>
									Key Findings
								</h4>
								<ul class="list-disc list-inside text-sm space-y-1">
									{#each review.findings.slice(0, 5) as finding}
										<li>{finding}</li>
									{/each}
									{#if review.findings.length > 5}
										<li class="opacity-60">...and {review.findings.length - 5} more</li>
									{/if}
								</ul>
							</div>
						{/if}

						<!-- Enrichments -->
						{#if hasEnrichments(review)}
							<details class="mb-4">
								<summary class="font-bold text-sm mb-2 flex items-center gap-2 cursor-pointer">
									<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
									</svg>
									Enrichment Results
								</summary>
								<div class="bg-surface-500/10 rounded p-2 max-h-32 overflow-y-auto mt-2">
									<pre class="text-xs whitespace-pre-wrap">{JSON.stringify(review.enrichments, null, 2)}</pre>
								</div>
							</details>
						{/if}

						<!-- MISP Context -->
						{#if hasMispContext(review)}
							<details class="mb-4">
								<summary class="font-bold text-sm mb-2 flex items-center gap-2 cursor-pointer">
									<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
									</svg>
									MISP Threat Intel
								</summary>
								<div class="bg-surface-500/10 rounded p-2 max-h-32 overflow-y-auto mt-2">
									<pre class="text-xs whitespace-pre-wrap">{JSON.stringify(review.misp_context, null, 2)}</pre>
								</div>
							</details>
						{/if}

						<!-- Action Area -->
						{#if selectedReview?.id === review.id}
							<div class="border-t border-surface-500/20 pt-4 mt-4">
								<label class="label mb-3">
									<span>Analyst Feedback (required for rejection)</span>
									<textarea
										class="textarea"
										rows="3"
										placeholder="Add your feedback, analysis notes, or reasoning..."
										bind:value={feedback}
									></textarea>
								</label>
								<div class="flex flex-wrap gap-2 justify-end">
									<button
										class="btn variant-soft"
										on:click={() => { selectedReview = null; feedback = ''; }}
										disabled={processing}
									>
										Cancel
									</button>
									<button
										class="btn variant-soft-secondary"
										on:click={() => { showRequestInfoModal = true; }}
										disabled={processing}
									>
										<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
										</svg>
										Request Info
									</button>
									<button
										class="btn variant-filled-error"
										on:click={() => rejectReview(review.id)}
										disabled={processing}
									>
										<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
										</svg>
										Reject & Close
									</button>
									<button
										class="btn variant-filled-success"
										on:click={() => approveReview(review.id)}
										disabled={processing}
									>
										<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
										</svg>
										Approve & Escalate
									</button>
								</div>
							</div>
						{:else}
							<div class="flex justify-end pt-2">
								<button
									class="btn variant-filled-primary"
									on:click={() => selectedReview = review}
								>
									<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
									</svg>
									Take Action
								</button>
							</div>
						{/if}
					</div>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<!-- Request Info Modal -->
{#if showRequestInfoModal && selectedReview}
	<div class="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
		<div class="card p-6 w-full max-w-lg m-4 max-h-[90vh] overflow-y-auto">
			<h3 class="h3 mb-4">Request Additional Information</h3>
			<p class="text-sm opacity-80 mb-4">
				Add questions or requests for additional analysis. The system will gather more information and update the review.
			</p>

			<div class="space-y-3 mb-4">
				{#each requestInfoQuestions as question, i}
					<div class="flex gap-2">
						<input
							type="text"
							class="input flex-1"
							placeholder="Enter your question or request..."
							bind:value={requestInfoQuestions[i]}
						/>
						{#if requestInfoQuestions.length > 1}
							<button
								class="btn-icon variant-soft-error"
								on:click={() => removeQuestion(i)}
							>
								<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						{/if}
					</div>
				{/each}
			</div>

			<button
				class="btn btn-sm variant-soft mb-4"
				on:click={addQuestion}
			>
				<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
				</svg>
				Add Another Question
			</button>

			<div class="flex justify-end gap-2">
				<button
					class="btn variant-soft"
					on:click={() => { showRequestInfoModal = false; requestInfoQuestions = ['']; }}
					disabled={processing}
				>
					Cancel
				</button>
					<button
						class="btn variant-filled-primary"
						on:click={() => selectedReview && requestMoreInfo(selectedReview.id)}
						disabled={processing}
					>
						{#if processing}
							<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
						{/if}
						Send Request
					</button>
				</div>
			</div>
	</div>
{/if}
