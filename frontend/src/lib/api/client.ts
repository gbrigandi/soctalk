/**
 * API client for SocTalk backend.
 */

const API_BASE = '/api';

export class ApiError extends Error {
	constructor(
		public status: number,
		public statusText: string,
		message: string
	) {
		super(message);
		this.name = 'ApiError';
	}
}

export interface AuthUser {
	username: string;
	roles: string[];
	source: string;
}

export interface AuthSession {
	enabled: boolean;
	mode: 'none' | 'static' | 'proxy';
	user: AuthUser | null;
}

export interface LoginRequest {
	username: string;
	password: string;
}

export interface LoginResponse {
	user: AuthUser;
}

async function request<T>(
	endpoint: string,
	options: RequestInit = {}
): Promise<T> {
	const url = `${API_BASE}${endpoint}`;
	const response = await fetch(url, {
		...options,
		headers: {
			'Content-Type': 'application/json',
			...options.headers
		}
	});

	if (!response.ok) {
		let message = '';
		const contentType = response.headers.get('content-type') || '';
		if (contentType.includes('application/json')) {
			try {
				const body = (await response.json()) as { detail?: unknown };
				message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body);
			} catch {
				message = await response.text();
			}
		} else {
			message = await response.text();
		}
		throw new ApiError(
			response.status,
			response.statusText,
			message || `Request failed: ${response.statusText}`
		);
	}

	return response.json();
}

export interface InvestigationSummary {
	id: string;
	title: string | null;
	status: string;
	phase: string;
	created_at: string;
	updated_at: string;
	closed_at: string | null;
	alert_count: number;
	observable_count: number;
	malicious_count: number;
	suspicious_count: number;
	clean_count: number;
	max_severity: string | null;
	verdict_decision: string | null;
	thehive_case_id: string | null;
}

export interface Investigation extends InvestigationSummary {
	time_to_triage_seconds: number | null;
	time_to_verdict_seconds: number | null;
	verdict_confidence: number | null;
	verdict_reasoning: string | null;
	threat_actor: string | null;
	tags: string[];
}

export interface InvestigationList {
	items: InvestigationSummary[];
	total: number;
	page: number;
	page_size: number;
	has_more: boolean;
}

export interface InvestigationTimelineEvent {
	id: string;
	event_type: string;
	timestamp: string;
	data: Record<string, unknown>;
}

export interface ActionResponse {
	success: boolean;
	message: string;
	investigation_id: string;
}

export interface EventTimelineResponse {
	investigation_id: string;
	events: InvestigationTimelineEvent[];
}

export interface MetricsOverview {
	open_investigations: number;
	pending_reviews: number;
	investigations_created_today: number;
	investigations_closed_today: number;
	escalations_today: number;
	auto_closed_today: number;
	avg_time_to_triage_seconds: number | null;
	avg_time_to_verdict_seconds: number | null;
	total_alerts_today: number;
	total_observables_today: number;
	malicious_observables_today: number;
	severity_breakdown: Record<string, number>;
	verdict_breakdown: Record<string, number>;
}

export interface HourlyMetric {
	hour: string;
	investigations_created: number;
	investigations_closed: number;
	escalations: number;
	auto_closed: number;
	avg_time_to_verdict_seconds: number | null;
	total_alerts: number;
	total_observables: number;
	malicious_observables: number;
	open_wip: number;
}

export interface HourlyMetricsResponse {
	metrics: HourlyMetric[];
	start: string;
	end: string;
	total_hours: number;
}

export interface PendingReview {
	id: string;
	investigation_id: string;
	status: string;
	title: string;
	description: string;
	max_severity: string;
	alert_count: number;
	malicious_count: number;
	suspicious_count: number;
	clean_count: number;
	findings: string[];
	enrichments: Record<string, unknown>;
	misp_context: Record<string, unknown> | null;
	ai_decision: string | null;
	ai_confidence: number | null;
	ai_assessment: string | null;
	ai_recommendation: string | null;
	timeout_seconds: number;
	created_at: string;
	expires_at: string | null;
}

export interface PendingReviewList {
	items: PendingReview[];
	total: number;
	page: number;
	page_size: number;
	has_more: boolean;
}

export interface AuditEvent {
	id: string;
	aggregate_id: string;
	aggregate_type: string;
	event_type: string;
	version: number;
	timestamp: string;
	data: Record<string, unknown>;
	metadata: Record<string, unknown>;
}

export interface AuditEventList {
	items: AuditEvent[];
	total: number;
	page: number;
	page_size: number;
	has_more: boolean;
}

export interface IOCStatItem {
	id: string;
	value: string;
	type: string;
	times_seen: number;
	last_seen: string;
	malicious_count: number;
	benign_count: number;
	threat_actors: string[];
	malicious_rate: number;
}

export interface IOCStatsResponse {
	items: IOCStatItem[];
	total: number;
	page: number;
	page_size: number;
	has_more: boolean;
}

export interface RuleStatItem {
	rule_id: string;
	times_triggered: number;
	escalation_count: number;
	auto_close_count: number;
	precision_rate: number | null;
	escalation_rate: number;
}

export interface RuleStatsResponse {
	items: RuleStatItem[];
	total: number;
}

export interface AnalyzerStatItem {
	analyzer: string;
	invocations: number;
	successes: number;
	failures: number;
	avg_response_time_ms: number | null;
	success_rate: number;
}

export interface AnalyzerStatsResponse {
	items: AnalyzerStatItem[];
	total: number;
}

// Analytics Types
export interface ExecutiveKPIs {
	auto_close_rate: number;
	escalation_rate: number;
	human_override_rate: number;
	mean_time_to_decision_seconds: number | null;
	total_investigations: number;
	auto_closed_count: number;
	escalated_count: number;
	human_reviewed_count: number;
	avg_ai_confidence: number | null;
	high_confidence_rate: number;
}

export interface ConfidenceBucket {
	range_label: string;
	count: number;
	percentage: number;
}

export interface DecisionTrend {
	period: string;
	close: number;
	escalate: number;
	needs_more_info: number;
	suspicious: number;
}

export interface EscalationReason {
	reason: string;
	count: number;
	percentage: number;
}

export interface AIBehavior {
	confidence_distribution: ConfidenceBucket[];
	decision_trends: DecisionTrend[];
	escalation_breakdown: EscalationReason[];
	avg_confidence_by_decision: Record<string, number>;
}

export interface HumanReviewStats {
	total_reviews: number;
	approved: number;
	rejected: number;
	info_requested: number;
	expired: number;
	pending: number;
	approval_rate: number;
	rejection_rate: number;
	avg_review_time_seconds: number | null;
	ai_agreed_count: number;
	ai_overridden_count: number;
	override_rate: number;
}

export interface OutcomeMetrics {
	total_closed: number;
	avg_resolution_time_seconds: number | null;
	p50_resolution_time_seconds: number | null;
	p90_resolution_time_seconds: number | null;
	closed_as_false_positive: number;
	closed_as_true_positive: number;
	closed_as_suspicious: number;
	reopen_rate: number;
}

export interface AnalyticsSummary {
	period_start: string;
	period_end: string;
	executive_kpis: ExecutiveKPIs;
	ai_behavior: AIBehavior;
	human_review: HumanReviewStats;
	outcomes: OutcomeMetrics;
}

// API methods
export const api = {
	auth: {
		session: () => request<AuthSession>('/auth/session'),

		login: (payload: LoginRequest) =>
			request<LoginResponse>('/auth/login', {
				method: 'POST',
				body: JSON.stringify(payload)
			}),

		logout: () =>
			request<{ success: boolean }>('/auth/logout', {
				method: 'POST'
			})
	},

	// Investigations
	investigations: {
		list: (params?: {
			page?: number;
			page_size?: number;
			status?: string;
			phase?: string;
			severity?: string;
		}) => {
			const query = new URLSearchParams();
			if (params?.page) query.set('page', String(params.page));
			if (params?.page_size) query.set('page_size', String(params.page_size));
			if (params?.status) query.set('status', params.status);
			if (params?.phase) query.set('phase', params.phase);
			if (params?.severity) query.set('severity', params.severity);
			const qs = query.toString();
			return request<InvestigationList>(`/investigations${qs ? `?${qs}` : ''}`);
		},

		get: (id: string) => request<Investigation>(`/investigations/${id}`),

		getEvents: async (id: string, limit?: number): Promise<InvestigationTimelineEvent[]> => {
			const query = limit ? `?limit=${limit}` : '';
			const response = await request<EventTimelineResponse>(`/investigations/${id}/events${query}`);
			return response.events;
		},

		pause: (id: string) => request<ActionResponse>(`/investigations/${id}/pause`, { method: 'POST' }),

		resume: (id: string) => request<ActionResponse>(`/investigations/${id}/resume`, { method: 'POST' }),

		cancel: (id: string, reason?: string) =>
			request<ActionResponse>(`/investigations/${id}/cancel`, {
				method: 'POST',
				body: JSON.stringify({ reason })
			})
	},

	// Human Review
	review: {
		listPending: (params?: { page?: number; page_size?: number }) => {
			const query = new URLSearchParams();
			if (params?.page) query.set('page', String(params.page));
			if (params?.page_size) query.set('page_size', String(params.page_size));
			const qs = query.toString();
			return request<PendingReviewList>(`/review/pending${qs ? `?${qs}` : ''}`);
		},

		get: (id: string) => request<PendingReview>(`/review/${id}`),

		approve: (id: string, feedback?: string) =>
			request<{ success: boolean }>(`/review/${id}/approve`, {
				method: 'POST',
				body: JSON.stringify({ feedback })
			}),

		reject: (id: string, feedback?: string) =>
			request<{ success: boolean }>(`/review/${id}/reject`, {
				method: 'POST',
				body: JSON.stringify({ feedback })
			}),

		requestInfo: (id: string, questions: string[]) =>
			request<{ success: boolean }>(`/review/${id}/request-info`, {
				method: 'POST',
				body: JSON.stringify({ questions })
			})
	},

	// Metrics
	metrics: {
		overview: () => request<MetricsOverview>('/metrics/overview'),

		hourly: (hours?: number) => {
			const query = hours ? `?hours=${hours}` : '';
			return request<HourlyMetricsResponse>(`/metrics/hourly${query}`);
		}
	},

	// Stats
	stats: {
		iocs: (params?: {
			page?: number;
			page_size?: number;
			type?: string;
			malicious_only?: boolean;
			sort_by?: string;
		}) => {
			const query = new URLSearchParams();
			if (params?.page) query.set('page', String(params.page));
			if (params?.page_size) query.set('page_size', String(params.page_size));
			if (params?.type) query.set('type', params.type);
			if (params?.malicious_only) query.set('malicious_only', 'true');
			if (params?.sort_by) query.set('sort_by', params.sort_by);
			const qs = query.toString();
			return request<IOCStatsResponse>(`/stats/iocs${qs ? `?${qs}` : ''}`);
		},

		rules: (params?: { limit?: number; sort_by?: string }) => {
			const query = new URLSearchParams();
			if (params?.limit) query.set('limit', String(params.limit));
			if (params?.sort_by) query.set('sort_by', params.sort_by);
			const qs = query.toString();
			return request<RuleStatsResponse>(`/stats/rules${qs ? `?${qs}` : ''}`);
		},

		analyzers: (sort_by?: string) => {
			const query = sort_by ? `?sort_by=${sort_by}` : '';
			return request<AnalyzerStatsResponse>(`/stats/analyzers${query}`);
		}
	},

	// Analytics
	analytics: {
		summary: (days?: number) => {
			const query = days ? `?days=${days}` : '';
			return request<AnalyticsSummary>(`/analytics/summary${query}`);
		},

		kpis: (days?: number) => {
			const query = days ? `?days=${days}` : '';
			return request<ExecutiveKPIs>(`/analytics/kpis${query}`);
		},

		aiBehavior: (days?: number) => {
			const query = days ? `?days=${days}` : '';
			return request<AIBehavior>(`/analytics/ai-behavior${query}`);
		},

		humanReview: (days?: number) => {
			const query = days ? `?days=${days}` : '';
			return request<HumanReviewStats>(`/analytics/human-review${query}`);
		},

		outcomes: (days?: number) => {
			const query = days ? `?days=${days}` : '';
			return request<OutcomeMetrics>(`/analytics/outcomes${query}`);
		}
	},

	// Audit
	audit: {
		list: (params?: {
			page?: number;
			page_size?: number;
			event_type?: string;
			aggregate_type?: string;
			start_date?: string;
			end_date?: string;
			investigation_id?: string;
		}) => {
			const query = new URLSearchParams();
			if (params?.page) query.set('page', String(params.page));
			if (params?.page_size) query.set('page_size', String(params.page_size));
			if (params?.event_type) query.set('event_type', params.event_type);
			if (params?.aggregate_type) query.set('aggregate_type', params.aggregate_type);
			if (params?.start_date) query.set('start_date', params.start_date);
			if (params?.end_date) query.set('end_date', params.end_date);
			if (params?.investigation_id) query.set('investigation_id', params.investigation_id);
			const qs = query.toString();
			return request<AuditEventList>(`/audit${qs ? `?${qs}` : ''}`);
		},

		getInvestigationAudit: (investigationId: string, limit?: number) => {
			const query = limit ? `?limit=${limit}` : '';
			return request<{
				investigation_id: string;
				title: string | null;
				status: string;
				phase: string;
				created_at: string;
				events: AuditEvent[];
				total_events: number;
			}>(`/audit/investigation/${investigationId}${query}`);
		},

		getEventTypes: () => request<{ event_types: string[] }>('/audit/event-types'),

		getStats: (hours?: number) => {
			const query = hours ? `?hours=${hours}` : '';
			return request<{
				period_hours: number;
				total_events: number;
				unique_investigations: number;
				events_by_type: Record<string, number>;
				events_by_hour: Record<string, number>;
			}>(`/audit/stats${query}`);
		}
	},

	settings: {
		get: () => request<Settings>('/settings'),

		update: (updates: SettingsUpdate) =>
			request<Settings>('/settings', {
				method: 'PUT',
				body: JSON.stringify(updates)
			}),

		reset: () =>
			request<Settings>('/settings/reset', {
				method: 'POST'
			})
	}
	};

// Settings - Integration configurations for MCP servers
export interface Settings {
	id: string;
	readonly: boolean;
	sources: Record<string, 'env' | 'db'>;

	// LLM settings (non-secret; keys are env-only)
	llm_provider: 'anthropic' | 'openai';
	llm_fast_model: string;
	llm_reasoning_model: string;
	llm_temperature: number;
	llm_max_tokens: number;
	llm_anthropic_base_url: string | null;
	llm_openai_base_url: string | null;
	llm_openai_organization: string | null;
	anthropic_api_key_configured: boolean;
	openai_api_key_configured: boolean;
	llm_keys_conflict: boolean;

	// Wazuh SIEM integration
	wazuh_enabled: boolean;
	wazuh_url: string | null;
	wazuh_verify_ssl: boolean;
	wazuh_credentials_configured: boolean;

	// Cortex integration
	cortex_enabled: boolean;
	cortex_url: string | null;
	cortex_verify_ssl: boolean;
	cortex_api_key_configured: boolean;

	// TheHive integration
	thehive_enabled: boolean;
	thehive_url: string | null;
	thehive_organisation: string | null;
	thehive_verify_ssl: boolean;
	thehive_api_key_configured: boolean;

	// MISP integration
	misp_enabled: boolean;
	misp_url: string | null;
	misp_verify_ssl: boolean;
	misp_api_key_configured: boolean;

	// Slack integration
	slack_enabled: boolean;
	slack_channel: string | null;
	slack_notify_on_escalation: boolean;
	slack_notify_on_verdict: boolean;
	slack_webhook_configured: boolean;

	updated_at: string;
}

export interface SettingsUpdate {
	// LLM settings (non-secret; keys are env-only)
	llm_provider?: 'anthropic' | 'openai';
	llm_fast_model?: string;
	llm_reasoning_model?: string;
	llm_temperature?: number;
	llm_max_tokens?: number;
	llm_anthropic_base_url?: string | null;
	llm_openai_base_url?: string | null;
	llm_openai_organization?: string | null;

	// Wazuh SIEM integration
	wazuh_enabled?: boolean;
	wazuh_url?: string | null;
	wazuh_verify_ssl?: boolean;

	// Cortex integration
	cortex_enabled?: boolean;
	cortex_url?: string | null;
	cortex_verify_ssl?: boolean;

	// TheHive integration
	thehive_enabled?: boolean;
	thehive_url?: string | null;
	thehive_organisation?: string | null;
	thehive_verify_ssl?: boolean;

	// MISP integration
	misp_enabled?: boolean;
	misp_url?: string | null;
	misp_verify_ssl?: boolean;

	// Slack integration
	slack_enabled?: boolean;
	slack_channel?: string | null;
	slack_notify_on_escalation?: boolean;
	slack_notify_on_verdict?: boolean;
}

export default api;
