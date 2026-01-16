/**
 * Svelte stores for global state management.
 */

import { writable, derived, type Readable } from 'svelte/store';
import type { AuthSession } from '$lib/api/client';

export const authSession = writable<AuthSession>({
	enabled: false,
	mode: 'none',
	user: null
});

export const isAuthenticated: Readable<boolean> = derived(authSession, ($session) => {
	if (!$session.enabled) return true;
	return $session.user !== null;
});

export const canReview: Readable<boolean> = derived(authSession, ($session) => {
	if (!$session.enabled) return true;
	const roles = $session.user?.roles ?? [];
	return roles.includes('admin') || roles.includes('analyst');
});

export const canEditSettings: Readable<boolean> = derived(authSession, ($session) => {
	if (!$session.enabled) return true;
	const roles = $session.user?.roles ?? [];
	return roles.includes('admin');
});

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
	sseError.set('Connection lost. Reconnecting...');

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
				id: data.id || crypto.randomUUID(),
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
	const id = crypto.randomUUID();
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
			title: 'New Investigation',
			message: `Investigation started: ${event.data.title || 'Untitled'}`
		});
	} else if (eventType === 'investigation.closed') {
		addToast({
			type: 'success',
			title: 'Investigation Closed',
			message: `Investigation closed with verdict: ${event.data.verdict || 'unknown'}`
		});
	}

	// Human review events
	else if (eventType === 'human.review_requested') {
		addToast({
			type: 'warning',
			title: 'Review Required',
			message: 'A new investigation requires human review',
			duration: 10000
		});
		// Update pending count
		pendingReviewsCount.update((n) => n + 1);
	} else if (eventType === 'human.decision_received') {
		addToast({
			type: 'success',
			title: 'Review Complete',
			message: `Review decision: ${event.data.decision}`
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
			title: 'Verdict Rendered',
			message: `AI verdict: ${verdict} (confidence: ${Math.round((event.data.confidence as number || 0) * 100)}%)`
		});
	}

	// TheHive events
	else if (eventType === 'thehive.case_created') {
		addToast({
			type: 'success',
			title: 'Case Created',
			message: `TheHive case created: ${event.data.case_id}`
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
