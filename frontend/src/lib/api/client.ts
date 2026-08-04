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
	user_id: string;
	email: string;
	user_type: string;
	role: string;
	tenant_id: string | null;
	current_tenant: string | null;
	current_tenant_slug?: string | null;
	current_tenant_display_name?: string | null;
	/** Capabilities the role holds (from the backend role→permission map). The UI gates
	 * nav and actions on these rather than guessing from the role string. */
	permissions?: string[];
}

export interface AuthSession {
	enabled: boolean;
	mode: 'none' | 'internal' | 'proxy';
	user: AuthUser | null;
}

export interface LoginRequest {
	email: string;
	password: string;
	tenant_slug?: string | null;
}

export interface PublicTenantBranding {
	app_name: string;
	logo_url: string | null;
	primary_color: string | null;
	secondary_color: string | null;
	favicon_url: string | null;
}

export interface PublicTenant {
	id: string;
	slug: string;
	display_name: string;
	state: string;
	branding: PublicTenantBranding;
}

export interface PublicMssp {
	id: string;
	slug: string;
	display_name: string;
	branding: PublicTenantBranding;
}

export interface PublicScope {
	kind: 'mssp' | 'tenant';
	slug: string;
	id: string;
	display_name: string;
	branding: PublicTenantBranding;
	state?: string;
}

export interface LoginResponse {
	user: AuthUser;
	must_change: boolean;
}

// Tenant-side BYOK shape. ``has_api_key=false`` means the tenant is
// using the MSSP shared key (no override set); the runs-worker still
// works, just on the MSSP's bill. ``api_key_preview`` carries a
// masked tail (e.g. ``sk-ant-…ABCD``) when an override is set —
// no plaintext leak, but enough for the tenant to confirm WHICH key
// is in use. Provider/model are MSSP-controlled and read-only here.
export interface TenantLlmConfig {
	provider: string;
	base_url: string;
	model: string;
	has_api_key: boolean;
	api_key_preview: string;
}

export interface RunBudget {
	install_default: number;
	install_max: number;
	tenant_override: number | null;
	effective: number;
	spend_24h_tokens: number;
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
		// Bootstrap admins are gated by ``must_change=true``: the auth
		// middleware rejects non-whitelisted calls with this exact
		// detail. If the SPA hits one of those, route the browser to
		// /account/password so the user can clear the flag — otherwise
		// every page would surface error toasts with no way forward.
		if (
			response.status === 403 &&
			message === 'password_change_required' &&
			typeof window !== 'undefined' &&
			!window.location.pathname.startsWith('/account/password')
		) {
			window.location.assign('/account/password?must_change=1');
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
	tenant_id?: string | null;
	tenant_slug?: string | null;
	tenant_display_name?: string | null;
}

export interface Investigation extends InvestigationSummary {
	time_to_triage_seconds: number | null;
	time_to_verdict_seconds: number | null;
	verdict_confidence: number | null;
	verdict_reasoning: string | null;
	threat_actor: string | null;
	tags: string[];
	tokens_used: number | null;
	tokens_budget: number | null;
	disposition: string | null;
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
	// Replay/cursor fields (#72); absent on older servers.
	seq?: number | null;
	run_id?: string | null;
	visibility?: string | null;
}

export interface ActionResponse {
	success: boolean;
	message: string;
	investigation_id: string;
}

export interface EventTimelineResponse {
	investigation_id: string;
	events: InvestigationTimelineEvent[];
	// Replay/cursor envelope (#72); absent on older servers.
	server_now?: string | null;
	next_after_seq?: number | null;
	has_more?: boolean;
}

// Fleet flight-recorder wire DTOs (#72). These mirror
// core/api/fleet_day.py response models; view-only types live in
// $lib/pipeline-viz.
export interface FleetDot {
	alert_id: string;
	investigation_id: string | null;
	first_event_at: string;
	closed_at: string | null;
	path: string | null;
	outcome: 'closed' | 'human' | 'closed_unrecorded' | 'open';
	veto: boolean;
}

export interface FleetVetoRow {
	investigation_id: string;
	at: string;
	stage: string | null;
	fired: string[];
}

export interface FleetDay {
	date: string;
	tz: string;
	server_now: string;
	window_start: string;
	window_end: string;
	ingested: number;
	closed_ingest_memoized: number;
	closed_ingest_rules: number;
	closed_operational: number;
	closed_reasoning: number;
	escalated: number;
	guard_vetoes: number;
	still_open: number;
	ingest_histogram: number[];
	dollars_used: number;
	tokens_used: number;
	sample_rate: number;
	dots: FleetDot[];
	recent_vetoes: FleetVetoRow[];
}

export interface FleetArrival {
	alert_id: string;
	investigation_id: string | null;
	first_event_at: string;
	status: string | null;
}

export interface FleetLive {
	server_now: string;
	window_start: string;
	ingested: number;
	closed_ingest_memoized: number;
	closed_ingest_rules: number;
	closed_operational: number;
	closed_reasoning: number;
	escalated: number;
	guard_vetoes: number;
	in_flight: number;
	last_alert_at: string | null;
	open_by_stage: Record<string, number>;
	recent_arrivals: FleetArrival[];
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
	tenant_id: string | null;
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

export interface TriagePolicyGuardrail {
	when: Record<string, unknown>;
	effect: string;
	to: string;
	reason: string;
}

export interface TriagePolicyMatch {
	rule_groups: string[];
	rule_ids: string[];
	authorization_tracks: string[];
}

export interface TriagePolicy {
	id: string;
	version: number;
	tenant: string;
	status: 'active' | 'shadow';
	priority: number;
	source: 'built-in' | 'file';
	applies_to: TriagePolicyMatch;
	required_steps: string[];
	decision_modules: string[];
	deterministic_disposition: string | null;
	legal_actions: Record<string, string[]>;
	close_signoff_data_classes: string[];
	guardrails: TriagePolicyGuardrail[];
}

export interface AuthoredTriagePolicy {
	triage_policy_id: string;
	revision: number;
	status: string;
	definition: Record<string, unknown>;
}

export interface AuthoredResponsePlaybook {
	response_playbook_id: string;
	revision: number;
	status: string;
	definition: Record<string, unknown>;
}

// API methods
export const api = {
	auth: {
		// /api/auth/me returns the current identity from the V1 backend.
		// Wrapped in AuthSession to keep the existing UI shape stable;
		// 401 → ``user: null``.
		session: async (): Promise<AuthSession> => {
			try {
				const user = await request<AuthUser>('/auth/me');
				return { enabled: true, mode: 'internal', user };
			} catch (err) {
				if (err instanceof ApiError && err.status === 401) {
					return { enabled: true, mode: 'internal', user: null };
				}
				throw err;
			}
		},

		login: (payload: LoginRequest) =>
			request<LoginResponse>('/auth/login', {
				method: 'POST',
				body: JSON.stringify(payload)
			}),

		logout: () =>
			request<{ success: boolean }>('/auth/logout', {
				method: 'POST'
			}),

		assumeTenant: (slug: string | null) =>
			request<AuthUser>('/auth/assume-tenant', {
				method: 'POST',
				body: JSON.stringify(slug ? { slug } : {})
			}),

		// Required after a tenant_admin's first login (must_change=true on
		// the bootstrap credential). Until cleared, the auth middleware
		// rejects every non-whitelisted call with 403 password_change_required.
		changePassword: (oldPassword: string, newPassword: string) =>
			request<{ success: boolean }>('/auth/password/change', {
				method: 'POST',
				body: JSON.stringify({
					old_password: oldPassword,
					new_password: newPassword
				})
			})
	},

	// Public, no auth — slug-driven landing.
	public: {
		// Unified resolver: pass any slug from a flat
		// ``<slug>.<base>`` hostname; backend decides MSSP vs tenant.
		scopeBySlug: (slug: string) =>
			request<PublicScope>(`/public/scope-by-slug/${encodeURIComponent(slug)}`),
		// Kept for callers that already know which kind they want.
		tenantBySlug: (slug: string) =>
			request<PublicTenant>(`/public/tenant-by-slug/${encodeURIComponent(slug)}`),
		msspBySlug: (slug: string) =>
			request<PublicMssp>(`/public/mssp-by-slug/${encodeURIComponent(slug)}`)
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

		// Ascending cursor feed for the flight recorder (#72). Returns the
		// full envelope: next_after_seq is the next poll's cursor and
		// server_now is the clock authority for the live head.
		getEventsCursor: (id: string, afterSeq = 0, limit = 500): Promise<EventTimelineResponse> => {
			const params = new URLSearchParams({
				after_seq: String(afterSeq),
				order: 'asc',
				limit: String(limit)
			});
			return request<EventTimelineResponse>(`/investigations/${id}/events?${params}`);
		},

		// Pause/resume intentionally omitted: the runs worker has no pause
		// semantics, so those routes do not exist (issue #16). Cancel is the
		// only wired lifecycle transition.
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

		// Lightweight live snapshot for the fleet panel (#72; polled 5-10s).
		getFleetLive: (opts?: { tz?: string }): Promise<FleetLive> => {
			const params = new URLSearchParams();
			if (opts?.tz) params.set('tz', opts.tz);
			const qs = params.toString();
			return request<FleetLive>(`/analytics/fleet-live${qs ? `?${qs}` : ''}`);
		},

		// Fleet-day aggregate for the flight recorder (#72). fallback:
		// 'latest_active' serves the most recent day with any alerts when
		// today is empty (only applies while date is omitted; the response
		// 'date' self-describes any substitution).
		getFleetDay: (opts?: {
			date?: string;
			tz?: string;
			sampleLimit?: number;
			fallback?: 'latest_active';
		}): Promise<FleetDay> => {
			const params = new URLSearchParams();
			if (opts?.date) params.set('date', opts.date);
			if (opts?.tz) params.set('tz', opts.tz);
			if (opts?.sampleLimit) params.set('sample_limit', String(opts.sampleLimit));
			if (opts?.fallback && !opts?.date) params.set('fallback', opts.fallback);
			const qs = params.toString();
			return request<FleetDay>(`/analytics/fleet-day${qs ? `?${qs}` : ''}`);
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
	},

	// Tenant-side BYOK for the LLM API key. The tenant_admin pastes
	// their own provider credential (Anthropic/OpenAI) which the
	// runs-worker then uses for THIS tenant's investigations instead
	// of the MSSP's shared install key. ``clearKey`` reverts to the
	// MSSP-funded default — the L1 controller re-mirrors the
	// install-shared key into the tenant ns so the runs-worker
	// keeps running uninterrupted.
	// Per-tenant run token budget (#103). DB-resolved at run creation, capped
	// at the install max; changes take effect with no worker rollout. MSSP
	// admins set/clear the override; the tenant view is read-only.
	runBudget: {
		get: (tenantId: string) =>
			request<RunBudget>(`/mssp/tenants/${tenantId}/run-budget`),
		update: (tenantId: string, override: number | null) =>
			request<RunBudget>(`/mssp/tenants/${tenantId}/run-budget`, {
				method: 'PATCH',
				body: JSON.stringify({ override })
			})
	},
	tenantRunBudget: {
		get: () => request<RunBudget>('/tenant/run-budget')
	},

	tenantLlm: {
		get: () => request<TenantLlmConfig>('/tenant/llm'),
		setKey: (apiKey: string) =>
			request<TenantLlmConfig>('/tenant/llm/api-key', {
				method: 'PUT',
				body: JSON.stringify({ api_key: apiKey })
			}),
		clearKey: () =>
			request<TenantLlmConfig>('/tenant/llm/api-key', {
				method: 'DELETE'
			})
	},

	// MSSP cross-tenant fleet dashboard. Only reachable when the
	// session has no current_tenant pin (cross-tenant scope).
	msspDashboard: {
		pendingReviews: () =>
			request<{ items: MsspPendingReviewRow[] }>('/mssp/dashboard/pending-reviews'),
		openByTenant: () =>
			request<{ items: MsspOpenByTenantRow[] }>('/mssp/dashboard/open-by-tenant'),
		stuckInvestigations: (hours = 8) =>
			request<{ items: MsspStuckInvestigationRow[] }>(
				`/mssp/dashboard/stuck-investigations?hours=${hours}`
			),
		tenantHealth: () =>
			request<{ items: MsspTenantHealthRow[] }>('/mssp/dashboard/tenant-health'),
		repeatedIocs: (days = 7, limit = 50) =>
			request<MsspRepeatedIocsResponse>(
				`/mssp/dashboard/repeated-iocs?days=${days}&limit=${limit}`
			)
	},

	// MSSP cross-tenant fleet analytics — trend-shaped, longitudinal.
	// Companion to msspDashboard but answers "is the practice
	// improving, degrading, or drifting?" not "where do I look now?".
	msspAnalytics: {
		trends: (days = 7) =>
			request<MsspTrendsResponse>(`/mssp/analytics/trends?days=${days}`),
		ranking: (
			metric: 'ttv' | 'ttr' = 'ttv',
			days = 30,
			minSample = 10,
			limit = 20
		) =>
			request<MsspRankingResponse>(
				`/mssp/analytics/ranking?metric=${metric}&days=${days}` +
					`&min_sample=${minSample}&limit=${limit}`
			),
		heatmap: (dimension: 'alerts' | 'cases' = 'alerts', days = 30) =>
			request<MsspHeatmapResponse>(
				`/mssp/analytics/heatmap?dimension=${dimension}&days=${days}`
			)
	},

	triagePolicies: {
		list: () => request<TriagePolicy[]>('/mssp/triage-policies'),
		listAuthored: (tenantId: string) =>
			request<AuthoredTriagePolicy[]>(`/mssp/tenants/${tenantId}/triage-policies`),
		createAuthored: (tenantId: string, definition: Record<string, unknown>, status = 'shadow') =>
			request<AuthoredTriagePolicy>(`/mssp/tenants/${tenantId}/triage-policies`, {
				method: 'POST',
				body: JSON.stringify({ definition, status })
			}),
		updateAuthored: (
			tenantId: string,
			triagePolicyId: string,
			definition: Record<string, unknown>,
			status = 'shadow'
		) =>
			request<AuthoredTriagePolicy>(`/mssp/tenants/${tenantId}/triage-policies/${triagePolicyId}`, {
				method: 'PUT',
				body: JSON.stringify({ definition, status })
			}),
		retireAuthored: (tenantId: string, triagePolicyId: string) =>
			request<{ ok: string }>(`/mssp/tenants/${tenantId}/triage-policies/${triagePolicyId}`, {
				method: 'DELETE'
			}),
		exportAuthored: (tenantId: string, triagePolicyId: string) =>
			request<{ triage_policy_id: string; yaml: string }>(
				`/mssp/tenants/${tenantId}/triage-policies/${triagePolicyId}/export`
			),
		activateAuthored: (tenantId: string, triagePolicyId: string) =>
			request<AuthoredTriagePolicy>(
				`/mssp/tenants/${tenantId}/triage-policies/${triagePolicyId}/activate`,
				{ method: 'POST' }
			),
		deactivateAuthored: (tenantId: string, triagePolicyId: string) =>
			request<AuthoredTriagePolicy>(
				`/mssp/tenants/${tenantId}/triage-policies/${triagePolicyId}/deactivate`,
				{ method: 'POST' }
			)
	},

	responsePlaybooks: {
		listAuthored: (tenantId: string) =>
			request<AuthoredResponsePlaybook[]>(`/mssp/tenants/${tenantId}/response-playbooks`),
		createAuthored: (tenantId: string, definition: Record<string, unknown>, status = 'shadow') =>
			request<AuthoredResponsePlaybook>(`/mssp/tenants/${tenantId}/response-playbooks`, {
				method: 'POST',
				body: JSON.stringify({ definition, status })
			}),
		updateAuthored: (
			tenantId: string,
			responsePlaybookId: string,
			definition: Record<string, unknown>,
			status = 'shadow'
		) =>
			request<AuthoredResponsePlaybook>(
				`/mssp/tenants/${tenantId}/response-playbooks/${responsePlaybookId}`,
				{ method: 'PUT', body: JSON.stringify({ definition, status }) }
			),
		retireAuthored: (tenantId: string, responsePlaybookId: string) =>
			request<{ ok: string }>(
				`/mssp/tenants/${tenantId}/response-playbooks/${responsePlaybookId}`,
				{ method: 'DELETE' }
			),
		exportAuthored: (tenantId: string, responsePlaybookId: string) =>
			request<{ response_playbook_id: string; yaml: string }>(
				`/mssp/tenants/${tenantId}/response-playbooks/${responsePlaybookId}/export`
			),
		activateAuthored: (tenantId: string, responsePlaybookId: string) =>
			request<AuthoredResponsePlaybook>(
				`/mssp/tenants/${tenantId}/response-playbooks/${responsePlaybookId}/activate`,
				{ method: 'POST' }
			),
		deactivateAuthored: (tenantId: string, responsePlaybookId: string) =>
			request<AuthoredResponsePlaybook>(
				`/mssp/tenants/${tenantId}/response-playbooks/${responsePlaybookId}/deactivate`,
				{ method: 'POST' }
			)
	},

	authorizationFacts: {
		list: (tenantId: string) =>
			request<{ facts: AuthorizationFact[] }>(
				`/mssp/tenants/${tenantId}/authorization/facts`
			),
		create: (tenantId: string, fact: Record<string, unknown>) =>
			request<{ stored: string }>(`/mssp/tenants/${tenantId}/authorization/facts`, {
				method: 'POST',
				body: JSON.stringify({ fact })
			}),
		revoke: (tenantId: string, factId: string, reason: string | null) =>
			request<{ revoked: string }>(
				`/mssp/tenants/${tenantId}/authorization/facts/${encodeURIComponent(factId)}/revoke`,
				{ method: 'POST', body: JSON.stringify({ reason }) }
			),
		// Promote (approve) / refuse (reject) a pending tenant-asserted fact.
		review: (tenantId: string, factId: string, decision: 'approve' | 'reject', reason?: string) =>
			request<{ reviewed: string; status: string }>(
				`/mssp/tenants/${tenantId}/authorization/facts/${encodeURIComponent(factId)}/review`,
				{ method: 'POST', body: JSON.stringify({ decision, reason: reason ?? null }) }
			),
		// Answer an ASK_AUTHORIZATION question affirmatively (epic M3): the answer is bound to a
		// genuine pending review; the server takes the activity/scope from the question, not the
		// client, mints a durable analyst_asserted grant, and resolves the review as benign.
		answer: (
			tenantId: string,
			body: {
				review_id: string;
				investigation_id: string;
				valid_until: string;
				reason?: string | null;
			}
		) =>
			request<{ stored: string; review_resolved: boolean }>(
				`/mssp/tenants/${tenantId}/authorization/answer`,
				{ method: 'POST', body: JSON.stringify(body) }
			)
	},
	// MSSP-side engagements — declared on a customer tenant's behalf (tenant in the path).
	// Same window+scope contract as the tenant self-service route below; the MSSP audience
	// needs authorize_engagement (SOC-manager tier) to declare or revoke.
	engagements: {
		list: (tenantId: string, includeRevoked = false) =>
			request<TenantEngagement[]>(
				`/mssp/tenants/${tenantId}/engagements${includeRevoked ? '?include_revoked=true' : ''}`
			),
		declare: (tenantId: string, body: TenantEngagementDeclare) =>
			request<{ id: string }>(`/mssp/tenants/${tenantId}/engagements`, {
				method: 'POST',
				body: JSON.stringify(body)
			}),
		revoke: (tenantId: string, engagementId: string, reason: string | null) =>
			request<{ ok: string }>(
				`/mssp/tenants/${tenantId}/engagements/${encodeURIComponent(engagementId)}/revoke`,
				{ method: 'POST', body: JSON.stringify({ reason }) }
			)
	},
	// Tenant self-service engagements — the caller's own tenant (from the token). Declaring a
	// pentest/red-team window+scope lets the SOC deconflict it.
	tenantEngagements: {
		list: (includeRevoked = false) =>
			request<TenantEngagement[]>(
				`/tenant/engagements${includeRevoked ? '?include_revoked=true' : ''}`
			),
		declare: (body: TenantEngagementDeclare) =>
			request<{ id: string }>('/tenant/engagements', {
				method: 'POST',
				body: JSON.stringify(body)
			}),
		revoke: (engagementId: string, reason: string | null) =>
			request<{ ok: string }>(
				`/tenant/engagements/${encodeURIComponent(engagementId)}/revoke`,
				{ method: 'POST', body: JSON.stringify({ reason }) }
			)
	},
	// Tenant self-service authorization facts — assert facts about your OWN org. They land
	// 'pending' (invisible to triage) until an MSSP analyst approves them.
	tenantAuthzFacts: {
		list: () => request<{ facts: AuthorizationFact[] }>('/tenant/authorization/facts'),
		assert: (fact: Record<string, unknown>) =>
			request<{ stored: string; review_status: string }>('/tenant/authorization/facts', {
				method: 'POST',
				body: JSON.stringify({ fact })
			})
	},
	// Tenant self-service user management (tenant_admin provisions its own org's logins).
	tenantUsers: {
		list: () => request<TenantUser[]>('/tenant/users'),
		create: (email: string, role: string, display_name?: string) =>
			request<TenantUserCreated>('/tenant/users', {
				method: 'POST',
				body: JSON.stringify({ email, role, display_name })
			}),
		update: (userId: string, patch: { role?: string; active?: boolean; display_name?: string }) =>
			request<TenantUser>(`/tenant/users/${userId}`, {
				method: 'PATCH',
				body: JSON.stringify(patch)
			}),
		deactivate: (userId: string) =>
			request<{ deactivated: string }>(`/tenant/users/${userId}/deactivate`, { method: 'POST' })
	},
	// MSSP-side staff user management (mssp_admin / platform_admin, MANAGE_USERS).
	msspUsers: {
		list: () => request<TenantUser[]>('/mssp/users'),
		create: (email: string, role: string, display_name?: string) =>
			request<TenantUserCreated>('/mssp/users', {
				method: 'POST',
				body: JSON.stringify({ email, role, display_name })
			}),
		update: (userId: string, patch: { role?: string; active?: boolean; display_name?: string }) =>
			request<TenantUser>(`/mssp/users/${userId}`, { method: 'PATCH', body: JSON.stringify(patch) }),
		deactivate: (userId: string) =>
			request<{ deactivated: string }>(`/mssp/users/${userId}/deactivate`, { method: 'POST' })
	}
	};

export interface TenantUser {
	id: string;
	email: string;
	display_name: string | null;
	role: string;
	active: boolean;
}

export interface TenantUserCreated extends TenantUser {
	temporary_password: string;
}

// Mirrors the server EngagementDTO. Note: there is NO `status` field on the
// wire — lifecycle (scheduled/active/expired/revoked) is derived client-side
// from starts_at/ends_at/revoked_at (see $lib/authz/display:engagementStatus).
export interface TenantEngagement {
	id: string;
	name: string;
	kind: string;
	starts_at: string;
	ends_at: string;
	scope_source_ips: string[];
	scope_hosts: string[];
	scope_techniques: string[];
	revoked_at: string | null;
	created_at: string;
	declared_test_count: number;
	out_of_scope_count: number;
}

export interface TenantEngagementDeclare {
	name: string;
	kind: string;
	starts_at: string;
	ends_at: string;
	scope_source_ips?: string[];
	scope_hosts?: string[];
	scope_techniques?: string[];
}

export interface AuthorizationFact {
	id: string;
	kind: 'grant' | 'prohibition' | 'change_freeze' | 'entity_context';
	track: 'account' | 'fim';
	source_type: string;
	trust: number;
	/** review lifecycle for tenant-asserted facts: pending | approved | rejected */
	review_status?: string;
	scope?: { subject?: string; target?: string; action?: string };
	valid_from?: string | null;
	valid_until?: string | null;
	created_by?: string;
	provenance?: { api_caller?: string | null; review_id?: string | null };
	[key: string]: unknown;
}

export interface MsspPendingReviewRow {
	tenant_id: string;
	slug: string;
	display_name: string;
	count: number;
}

export interface MsspOpenByTenantRow {
	tenant_id: string;
	slug: string;
	display_name: string;
	open_count: number;
	oldest_opened_at: string | null;
	max_severity: number | null;
}

export interface MsspStuckInvestigationRow {
	investigation_id: string;
	tenant_id: string;
	slug: string;
	display_name: string;
	opened_at: string;
	last_activity_at: string;
	severity: number;
	stuck_for_seconds: number;
}

export interface MsspTenantHealthRow {
	tenant_id: string;
	slug: string;
	display_name: string;
	state: string;
	last_heartbeat: string | null;
	heartbeat_age_seconds: number | null;
	unhealthy: boolean;
}

export interface MsspRepeatedIocRow {
	ioc_type: string;
	ioc_value: string;
	tenant_count: number;
	tenants: Array<{ id: string; slug: string; display_name: string }>;
	first_seen: string;
	last_seen: string;
	max_severity: number;
}

export interface MsspRepeatedIocsResponse {
	items: MsspRepeatedIocRow[];
	days: number;
	threshold: number;
}

export interface MsspTrendBucket {
	bucket: string;
	alert_count: number;
	closed_count: number;
	escalated_count: number;
	p95_ttv_seconds: number | null;
	p95_ttr_seconds: number | null;
}

export interface MsspTrendsResponse {
	days: number;
	bucket_size: 'hour' | 'day';
	buckets: MsspTrendBucket[];
	window_p95_ttv_seconds: number | null;
	window_p95_ttr_seconds: number | null;
	window_alert_total: number;
	window_closed_total: number;
	window_escalated_total: number;
}

export interface MsspRankingRow {
	tenant_id: string;
	slug: string;
	display_name: string;
	current_p95_seconds: number;
	previous_p95_seconds: number | null;
	delta_seconds: number | null;
	sample_current: number;
	sample_previous: number;
}

export interface MsspRankingResponse {
	metric: 'ttv' | 'ttr';
	days: number;
	min_sample: number;
	fleet_median_seconds: number | null;
	rows: MsspRankingRow[];
}

export interface MsspHeatmapCell {
	dow: number;  // 0=Sunday..6=Saturday
	hour: number; // 0..23
	count: number;
}

export interface MsspHeatmapResponse {
	dimension: 'alerts' | 'cases';
	days: number;
	cells: MsspHeatmapCell[];
}

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
