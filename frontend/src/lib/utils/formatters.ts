export function formatDecision(value: string | null | undefined): string {
	if (!value) return 'Unknown';

	// Remove enum prefixes like "VerdictDecision." or "HumanDecision."
	const clean = value.replace(/^(VerdictDecision|HumanDecision)\./i, '').toLowerCase();

	const map: Record<string, string> = {
		'escalate': 'Escalate',
		'close': 'Close',
		'auto_close': 'Auto-Close',
		'needs_more_info': 'Needs More Info',
		'suspicious': 'Suspicious',
		'approve': 'Approved',
		'approved': 'Approved',
		'reject': 'Rejected',
		'rejected': 'Rejected',
		'more_info': 'More Info Requested',
		'info_requested': 'More Info Requested',
		'expired': 'Expired',
		'pending': 'Pending',
		'unknown': 'Unknown',
	};

	return map[clean] || formatSnakeCase(value);
}

export function formatEventType(value: string | null | undefined): string {
	if (!value) return 'Unknown';

	const map: Record<string, string> = {
		'investigation.created': 'Investigation Started',
		'investigation.closed': 'Investigation Closed',
		'human.review_requested': 'Review Requested',
		'human.decision_received': 'Review Completed',
		'verdict.rendered': 'Verdict Rendered',
		'enrichment.completed': 'Enrichment Done',
		'enrichment.requested': 'Enrichment Started',
		'enrichment.failed': 'Enrichment Failed',
		'thehive.case_created': 'Case Created',
		'phase.changed': 'Phase Changed',
		'alert.correlated': 'Alert Added',
		'observable.extracted': 'Observable Found',
		'supervisor.decision': 'Supervisor Decision',
		'misp.context_retrieved': 'Threat Intel Retrieved',
		'wazuh.forensics_collected': 'Forensics Collected',
	};

	return map[value] || formatSnakeCase(value.replace(/[._]/g, ' '));
}

export function formatSeverity(value: string | null | undefined): string {
	if (!value) return 'Unknown';

	const map: Record<string, string> = {
		'critical': 'Critical',
		'high': 'High',
		'medium': 'Medium',
		'low': 'Low',
		'info': 'Info',
		'informational': 'Informational',
	};

	return map[value.toLowerCase()] || formatSnakeCase(value);
}

export function formatPhase(value: string | null | undefined): string {
	if (!value) return 'Unknown';

	const map: Record<string, string> = {
		'triage': 'Triage',
		'enrichment': 'Enrichment',
		'analysis': 'Analysis',
		'verdict': 'Verdict',
		'human_review': 'Human Review',
		'escalation': 'Escalation',
		'closed': 'Closed',
	};

	return map[value.toLowerCase()] || formatSnakeCase(value);
}

export function formatStatus(value: string | null | undefined): string {
	if (!value) return 'Unknown';

	const map: Record<string, string> = {
		'pending': 'Pending',
		'in_progress': 'In Progress',
		'paused': 'Paused',
		'escalated': 'Escalated',
		'auto_closed': 'Auto-Closed',
		'rejected': 'Rejected',
		'closed': 'Closed',
		'cancelled': 'Cancelled',
	};

	return map[value.toLowerCase()] || formatSnakeCase(value);
}

export function formatAction(value: string | null | undefined): string {
	if (!value) return 'Unknown';

	const map: Record<string, string> = {
		'INVESTIGATE': 'Investigate Further',
		'CLOSE': 'Close Investigation',
		'ESCALATE': 'Escalate to Incident',
		'ENRICH': 'Enrich Data',
		'WAIT': 'Wait for Input',
	};

	return map[value.toUpperCase()] || formatSnakeCase(value);
}

export function formatSnakeCase(value: string): string {
	return value
		.toLowerCase()
		.replace(/_/g, ' ')
		.replace(/\b\w/g, c => c.toUpperCase());
}

export function formatDuration(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined) return '-';
	if (seconds < 60) return `${Math.round(seconds)}s`;
	if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
	const hours = Math.floor(seconds / 3600);
	const mins = Math.round((seconds % 3600) / 60);
	return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

export function formatPercent(value: number | null | undefined, decimals = 1): string {
	if (value === null || value === undefined) return '-';
	return `${(value * 100).toFixed(decimals)}%`;
}

export function formatConfidence(value: number | null | undefined): string {
	if (value === null || value === undefined) return '-';
	const pct = value * 100;
	if (pct >= 90) return `${pct.toFixed(0)}% (Very High)`;
	if (pct >= 70) return `${pct.toFixed(0)}% (High)`;
	if (pct >= 50) return `${pct.toFixed(0)}% (Medium)`;
	if (pct >= 30) return `${pct.toFixed(0)}% (Low)`;
	return `${pct.toFixed(0)}% (Very Low)`;
}
