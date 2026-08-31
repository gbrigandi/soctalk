// Enum-code → localized display label (#52). The API returns stable codes;
// these lookups are the ONLY place codes become prose. Message functions are
// called at render time (never module scope), with formatSnakeCase as the
// fallback for codes the catalog doesn't know.
import { m } from '$lib/paraglide/messages';

export function formatDecision(value: string | null | undefined): string {
	if (!value) return m.common_unknown();

	// Remove enum prefixes like "VerdictDecision." or "HumanDecision."
	const clean = value.replace(/^(VerdictDecision|HumanDecision)\./i, '').toLowerCase();

	const map: Record<string, () => string> = {
		escalate: m.dec_escalate,
		close: m.dec_close,
		auto_close: m.dec_auto_close,
		needs_more_info: m.dec_needs_more_info,
		suspicious: m.dec_suspicious,
		approve: m.dec_approved,
		approved: m.dec_approved,
		reject: m.dec_rejected,
		rejected: m.dec_rejected,
		more_info: m.dec_more_info,
		info_requested: m.dec_more_info,
		expired: m.dec_expired,
		pending: m.dec_pending,
		unknown: m.common_unknown
	};

	return map[clean]?.() ?? formatSnakeCase(value);
}

// Every label below exists in the message catalog as dash_evt_*. This is THE
// event-type → message map: the Audit Log / timeline (via formatEventType) and
// the dashboard's badge renderer both read it, so a new event type is added
// exactly once. The dashboard keeps only its badge variants locally.
export const EVENT_TYPE_MESSAGES: Record<string, () => string> = {
	'investigation.created': m.dash_evt_investigation_created,
	'investigation.closed': m.dash_evt_investigation_closed,
	'human.review_requested': m.dash_evt_review_requested,
	'human.decision_received': m.dash_evt_review_completed,
	'verdict.rendered': m.dash_evt_verdict_rendered,
	'enrichment.completed': m.dash_evt_enrichment_done,
	'enrichment.requested': m.dash_evt_enrichment_started,
	'enrichment.failed': m.dash_evt_enrichment_failed,
	'thehive.case_created': m.dash_evt_case_created,
	'phase.changed': m.dash_evt_phase_changed,
	'alert.correlated': m.dash_evt_alert_added,
	'observable.extracted': m.dash_evt_observable_found,
	'supervisor.decision': m.dash_evt_supervisor_decision,
	'misp.context_retrieved': m.dash_evt_misp_intel,
	'wazuh.forensics_collected': m.dash_evt_forensics_collected
};

export function formatEventType(value: string | null | undefined): string {
	if (!value) return m.common_unknown();
	return EVENT_TYPE_MESSAGES[value]?.() ?? formatSnakeCase(value.replace(/[._]/g, ' '));
}

export function formatSeverity(value: string | null | undefined): string {
	if (!value) return m.common_unknown();

	const map: Record<string, () => string> = {
		critical: m.sev_critical,
		high: m.sev_high,
		medium: m.sev_medium,
		low: m.sev_low,
		info: m.sev_info,
		informational: m.sev_informational
	};

	return map[value.toLowerCase()]?.() ?? formatSnakeCase(value);
}

export function formatPhase(value: string | null | undefined): string {
	if (!value) return m.common_unknown();

	const map: Record<string, () => string> = {
		triage: m.phase_triage,
		enrichment: m.phase_enrichment,
		analysis: m.phase_analysis,
		verdict: m.phase_verdict,
		human_review: m.phase_human_review,
		escalation: m.phase_escalation,
		closed: m.phase_closed
	};

	return map[value.toLowerCase()]?.() ?? formatSnakeCase(value);
}

export function formatStatus(value: string | null | undefined): string {
	if (!value) return m.common_unknown();

	const map: Record<string, () => string> = {
		pending: m.st_pending,
		in_progress: m.st_in_progress,
		paused: m.st_paused,
		escalated: m.st_escalated,
		auto_closed: m.st_auto_closed,
		rejected: m.st_rejected,
		closed: m.st_closed,
		cancelled: m.st_cancelled
	};

	return map[value.toLowerCase()]?.() ?? formatSnakeCase(value);
}

export function formatAction(value: string | null | undefined): string {
	if (!value) return m.common_unknown();

	const map: Record<string, () => string> = {
		INVESTIGATE: m.act_investigate,
		CLOSE: m.act_close,
		ESCALATE: m.act_escalate,
		ENRICH: m.act_enrich,
		WAIT: m.act_wait
	};

	return map[value.toUpperCase()]?.() ?? formatSnakeCase(value);
}

export function formatSnakeCase(value: string): string {
	return value
		.toLowerCase()
		.replace(/_/g, ' ')
		.replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatDuration(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined) return '-';
	if (seconds < 60) return `${Math.round(seconds)}s`;
	if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
	const hours = Math.floor(seconds / 3600);
	const mins = Math.round((seconds % 3600) / 60);
	return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

export function formatRelativeAge(
	iso: string | null | undefined,
	nowMs: number
): string {
	if (!iso || !nowMs) return '-';
	return formatDuration(Math.max(0, (nowMs - Date.parse(iso)) / 1000));
}

export function formatPercent(value: number | null | undefined, decimals = 1): string {
	if (value === null || value === undefined) return '-';
	return `${(value * 100).toFixed(decimals)}%`;
}

export function formatConfidence(value: number | null | undefined): string {
	if (value === null || value === undefined) return '-';
	const pct = (value * 100).toFixed(0);
	const n = Number(pct);
	if (n >= 90) return m.conf_very_high({ pct });
	if (n >= 70) return m.conf_high({ pct });
	if (n >= 50) return m.conf_medium({ pct });
	if (n >= 30) return m.conf_low({ pct });
	return m.conf_very_low({ pct });
}
