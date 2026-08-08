/**
 * Svelte stores for global state management.
 */

import { writable, derived, type Readable } from 'svelte/store';
import type { AuthSession, PublicScope } from '$lib/api/client';
// Message functions are only ever CALLED inside event handlers (toast-creation
// time) — never at module scope, where the locale isn't set yet (#52).
import { m } from '$lib/paraglide/messages';
import { formatDecision } from '$lib/utils/formatters';

// Slug-driven landing — works for both ``<slug>.mssp.<base>`` and
// ``<slug>.customer.<base>``. The store carries a unified
// ``PublicScope`` regardless of kind so callers (login form, topbar,
// branded theming) don't branch on it.
//
// On the MSSP cross-tenant hostname (where the URL slug equals the
// install's MSSP slug) the store carries the MSSP identity; on a
// per-tenant hostname it carries the tenant. Both feed the same
// branding props. ``null`` when no slug detected (legacy hostnames).
export const tenantContext = writable<PublicScope | null>(null);

/**
 * Extract a slug from the leftmost label of the hostname.
 *
 *   labz.soctalk.ai       → 'labz'
 *   pw-kmo36q.soctalk.ai  → 'pw-kmo36q'
 *   localhost / IPs / 1-label → null
 *   100.102.223.8.nip.io  → null  (embedded IP, #136)
 *
 * Reserved subdomains (``www``, ``api``, ``cloud``, ``mssp``,
 * ``customer``, ``tenant``) return null. Resolution of the slug
 * to a kind (MSSP vs tenant) happens server-side via
 * /api/public/scope-by-slug.
 */
const RESERVED = new Set([
	'www',
	'api',
	'cloud',
	'mssp',
	'customer',
	'tenant',
	'admin',
	'app'
]);

export function detectSlugFromHostname(
	hostname: string | null | undefined
): string | null {
	if (!hostname) return null;
	const lower = hostname.toLowerCase();
	// Reject IPs and bare hostnames. The bare form is not enough: wildcard DNS
	// that embeds an address — nip.io, sslip.io, xip.io — is how a bare-IP
	// install gets a hostname, and `100.102.223.8.nip.io` is not a bare IP, so
	// it used to slip through and yield the slug `100` (#136).
	if (/^\d+\.\d+\.\d+\.\d+$/.test(lower)) return null;
	if (/^\d+\.\d+\.\d+\.\d+\./.test(lower)) return null;
	const parts = lower.split('.');
	if (parts.length < 2) return null;
	const slug = parts[0];
	if (RESERVED.has(slug)) return null;
	if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) return null;
	// An all-digit label is never a real slug, and is what an embedded-IP host
	// degrades to. Belt and braces with the check above (#136).
	if (/^\d+$/.test(slug)) return null;
	return slug;
}

export const authSession = writable<AuthSession>({
	enabled: false,
	mode: 'none',
	user: null
});

export const isAuthenticated: Readable<boolean> = derived(authSession, ($session) => {
	if (!$session.enabled) return true;
	return $session.user !== null;
});

// Capability gates. The backend puts a ``permissions`` array on /api/auth/me (derived from
// the role→permission map — the single source of truth). The UI gates on capabilities, not on
// role-string guesses, so a new role like ``mssp_manager`` works automatically.
export function hasPermission(perm: string): Readable<boolean> {
	return derived(authSession, ($session) => {
		if (!$session.enabled) return true; // auth off (dev): everything visible
		return ($session.user?.permissions ?? []).includes(perm);
	});
}

// Any-audience capability gate: true if the user holds the MSSP capability OR its tenant
// mirror. The co-managed-SOC operate surface (reviews, chat, triage) is reachable by an MSSP
// analyst AND a tenant_analyst, each under their own capability, so the UI gates on either.
export function hasAnyPermission(...perms: string[]): Readable<boolean> {
	return derived(authSession, ($session) => {
		if (!$session.enabled) return true;
		const held = $session.user?.permissions ?? [];
		return perms.some((p) => held.includes(p));
	});
}

// review a pending AI verdict (approve/reject/request-info) — MSSP analyst or tenant_analyst
export const canReview: Readable<boolean> = hasAnyPermission('review_decide', 'tenant_review_decide');
// use the AI chat/assistant — operators only (customer_viewer is read-only)
export const canChat: Readable<boolean> = hasAnyPermission('use_chat', 'tenant_use_chat');
// triage an investigation (comment / correct facts) — MSSP analyst or tenant_analyst
export const canTriageInvestigation: Readable<boolean> = hasAnyPermission(
	'triage_investigation',
	'tenant_triage_investigation'
);
// configure the system (any admin-tier capability) — Settings / integrations screens
export const canEditSettings: Readable<boolean> = derived(authSession, ($session) => {
	if (!$session.enabled) return true;
	const p = $session.user?.permissions ?? [];
	return (
		p.includes('configure_integrations') ||
		p.includes('manage_triage_policies') ||
		p.includes('manage_users')
	);
});
// author/activate custom triage policies (admin tier)
export const canManageTriagePolicies: Readable<boolean> = hasPermission('manage_triage_policies');
// curate authorization facts (SOC-manager tier)
export const canManageAuthorization: Readable<boolean> = hasPermission('manage_authorization_facts');
// see a customer tenant's engagements (analyst tier and up)
export const canViewEngagements: Readable<boolean> = hasPermission('view_engagements');
// declare/revoke engagements (SOC-manager tier)
export const canAuthorizeEngagements: Readable<boolean> = hasPermission('authorize_engagement');
// tenant self-service: view / declare own engagements (tenant viewer / tenant_manager)
export const canViewTenantEngagements: Readable<boolean> = hasPermission('tenant_view_engagements');
export const canDeclareTenantEngagement: Readable<boolean> = hasPermission(
	'tenant_authorize_engagement'
);
// tenant self-service: view / assert own authorization facts
export const canViewTenantAuthorization: Readable<boolean> = hasPermission(
	'tenant_view_authorization_facts'
);
export const canAssertTenantAuthorization: Readable<boolean> = hasPermission(
	'tenant_assert_authorization_facts'
);
// tenant self-service: manage own-org users + roles (tenant_admin)
export const canManageTenantUsers: Readable<boolean> = hasPermission('tenant_manage_users');
// MSSP-side: manage staff users + roles (mssp_admin / platform_admin)
export const canManageUsers: Readable<boolean> = hasPermission('manage_users');

// Whether the *user* is an MSSP-type identity (mssp_admin, mssp_analyst).
// Stable across "Open SOC" / "Clear" — pinning a tenant doesn't change
// the user's role, only the active scope. Use this to gate UI that the
// MSSP user always has, regardless of pin state — e.g. the "Clear" exit
// from a tenant pin must remain reachable while pinned.
export const isMsspUser: Readable<boolean> = derived(authSession, ($session) => {
	const ut = $session.user?.user_type ?? '';
	return ut === 'mssp' || ut.startsWith('mssp_');
});

// Whether the *active session scope* is cross-tenant MSSP. False while
// the user is pinned to a single tenant via "Open SOC". Use this to
// gate UI that only makes sense in cross-tenant context — e.g. the
// /tenants list and "All tenants" filters that would be confusing or
// out of scope under a tenant pin.
export const isMsspScope: Readable<boolean> = derived(authSession, ($session) => {
	const ut = $session.user?.user_type ?? '';
	if (!(ut === 'mssp' || ut.startsWith('mssp_'))) return false;
	if ($session.user?.current_tenant) return false;
	return true;
});

export const isCustomerScope: Readable<boolean> = derived(authSession, ($session) => {
	const role = $session.user?.role ?? '';
	return role === 'customer_viewer';
});

export const currentTenantId: Readable<string | null> = derived(
	authSession,
	($session) => $session.user?.current_tenant ?? $session.user?.tenant_id ?? null
);

// Types
export interface SSEEvent {
	id: string;
	type: string;
	data: Record<string, unknown>;
	timestamp: string;
}

export interface Toast {
	id: string;
	type: 'info' | 'success' | 'warning' | 'error';
	message: string;
	title?: string;
	duration?: number;
}

// SSE connection state
export const sseConnected = writable(false);
export const sseError = writable<string | null>(null);

// Recent events from SSE
export const recentEvents = writable<SSEEvent[]>([]);

// Toast notifications
export const toasts = writable<Toast[]>([]);

// Pending reviews count (for sidebar badge)
export const pendingReviewsCount = writable(0);

// SSE event source reference
let eventSource: EventSource | null = null;

// crypto.randomUUID() is gated behind secure-context (HTTPS or localhost).
// Lab installs run on plain HTTP via nginx-ingress, where Chrome stubs out
// crypto.randomUUID and any caller throws TypeError. Fall back to a Math
// generator on insecure origins — the values are only used as DOM keys
// for toasts and SSE event coalescing, not as cryptographic identifiers.
function safeUUID(): string {
	if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
		try {
			return crypto.randomUUID();
		} catch {
			/* fall through */
		}
	}
	// RFC 4122 v4-shaped string (Math.random — not crypto-strong, fine here).
	const hex = (n: number, l: number) => n.toString(16).padStart(l, '0');
	const r = () => Math.floor(Math.random() * 0x10000);
	return `${hex(r(), 4)}${hex(r(), 4)}-${hex(r(), 4)}-${hex((r() & 0x0fff) | 0x4000, 4)}-${hex((r() & 0x3fff) | 0x8000, 4)}-${hex(r(), 4)}${hex(r(), 4)}${hex(r(), 4)}`;
}

/**
 * Initialize SSE connection to the backend.
 */
export function initSSE(): void {
	if (typeof window === 'undefined') return;
	if (eventSource) return;

	eventSource = new EventSource('/api/events/stream');

eventSource.onopen = () => {
	sseConnected.set(true);
	sseError.set(null);
	if (import.meta.env.DEV) console.info('[SSE] Connected');
};

eventSource.onerror = (err) => {
	if (import.meta.env.DEV) console.error('[SSE] Error:', err);
	sseConnected.set(false);
	sseError.set(m.sse_connection_lost());

		// EventSource will auto-reconnect
	};

	// Listen for ping/heartbeat events to maintain connection status
	eventSource.addEventListener('ping', (event) => {
		// Ping received - connection is alive
		sseConnected.set(true);
		sseError.set(null);
	});

	eventSource.onmessage = (event) => {
		try {
			const data = JSON.parse(event.data);
			const sseEvent: SSEEvent = {
				id: data.id || safeUUID(),
				type: data.event_type || data.type || 'unknown',
				data: data.data || data,
				timestamp: data.timestamp || new Date().toISOString()
			};

			// Add to recent events (keep last 50)
			recentEvents.update((events) => {
				const updated = [sseEvent, ...events];
				return updated.slice(0, 50);
			});

			// Create toast for important events
			handleEventToast(sseEvent);

		} catch (e) {
			if (import.meta.env.DEV) console.error('[SSE] Failed to parse event:', e);
		}
	};
}

/**
 * Close SSE connection.
 */
export function closeSSE(): void {
	if (eventSource) {
		eventSource.close();
		eventSource = null;
		sseConnected.set(false);
	}
}

/**
 * Add a toast notification.
 */
export function addToast(toast: Omit<Toast, 'id'>): void {
	const id = safeUUID();
	const newToast: Toast = { id, ...toast };

	toasts.update((t) => [...t, newToast]);

	// Auto-remove after duration
	const duration = toast.duration ?? 5000;
	if (duration > 0) {
		setTimeout(() => removeToast(id), duration);
	}
}

/**
 * Remove a toast notification.
 */
export function removeToast(id: string): void {
	toasts.update((t) => t.filter((toast) => toast.id !== id));
}

/**
 * Handle toast creation for SSE events.
 */
function handleEventToast(event: SSEEvent): void {
	const eventType = event.type;

	// Investigation events
	if (eventType === 'investigation.created') {
		addToast({
			type: 'info',
			title: m.sse_new_investigation_title(),
			message: m.sse_investigation_started_msg({
				title: String(event.data.title || m.sse_untitled())
			})
		});
	} else if (eventType === 'investigation.closed') {
		addToast({
			type: 'success',
			title: m.sse_investigation_closed_title(),
			message: m.sse_investigation_closed_msg({
				verdict: String(event.data.verdict || 'unknown')
			})
		});
	}

	// Human review events
	else if (eventType === 'human.review_requested') {
		addToast({
			type: 'warning',
			title: m.sse_review_required_title(),
			message: m.sse_review_required_msg(),
			duration: 10000
		});
		// Update pending count
		pendingReviewsCount.update((n) => n + 1);
	} else if (eventType === 'human.decision_received') {
		addToast({
			type: 'success',
			title: m.sse_review_complete_title(),
			message: m.sse_review_decision_msg({
				decision: formatDecision(event.data.decision as string | undefined)
			})
		});
		pendingReviewsCount.update((n) => Math.max(0, n - 1));
	}

	// Verdict events
	else if (eventType === 'verdict.rendered') {
		const verdict = event.data.verdict || 'unknown';
		const toastType = verdict === 'malicious' ? 'error' :
		                   verdict === 'suspicious' ? 'warning' : 'info';
		addToast({
			type: toastType,
			title: m.sse_verdict_title(),
			message: m.sse_verdict_msg({
				verdict: String(verdict),
				pct: Math.round((event.data.confidence as number || 0) * 100)
			})
		});
	}

	// TheHive events
	else if (eventType === 'thehive.case_created') {
		addToast({
			type: 'success',
			title: m.sse_case_created_title(),
			message: m.sse_case_created_msg({ caseId: String(event.data.case_id) })
		});
	}
}

/**
 * Handle metrics updates from SSE events.
 */
// Derived store for SSE status display
export const sseStatus: Readable<{ connected: boolean; error: string | null }> = derived(
	[sseConnected, sseError],
	([$connected, $error]) => ({
		connected: $connected,
		error: $error
	})
);
