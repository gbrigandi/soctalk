<script lang="ts">
	import '../app.css';
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import { api } from '$lib/api/client';
	import { initSSE, closeSSE, sseStatus, pendingReviewsCount, authSession, canReview, isAuthenticated } from '$lib/stores';
	import Toast from '$lib/components/Toast.svelte';
	import { AppShell, AppBar, AppRail, AppRailAnchor, AppRailTile } from '@skeletonlabs/skeleton';

	// Navigation items
	const navItems = [
		{ href: '/', label: 'Dashboard', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6' },
		{ href: '/investigations', label: 'Investigations', icon: 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01' },
		{ href: '/review', label: 'Reviews', icon: 'M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z', badge: true },
		{ href: '/analytics', label: 'Analytics', icon: 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z' },
		{ href: '/audit', label: 'Audit Log', icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' },
		{ href: '/settings', label: 'Settings', icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z' }
	];

	let currentPath = '/';
	$: currentPath = $page.url.pathname;

	let visibleNavItems = navItems;
	let sseStarted = false;
	let authReady = false;

	$: if (authReady && !sseStarted && $isAuthenticated && currentPath !== '/login') {
		initSSE();
		sseStarted = true;
	}
	$: if (sseStarted && (!authReady || !$isAuthenticated)) {
		closeSSE();
		sseStarted = false;
	}

	onMount(async () => {
		try {
			const session = await api.auth.session();
			authSession.set(session);

			if (session.enabled && !session.user && currentPath !== '/login') {
				await goto('/login');
			}
		} catch (e) {
			// If auth/session fails, don't block rendering; API calls will surface errors.
			if (import.meta.env.DEV) console.error('[Auth] session check failed:', e);
		} finally {
			authReady = true;
		}
	});

	onDestroy(() => {
		closeSSE();
	});

	async function logout() {
		try {
			await api.auth.logout();
		} finally {
			authSession.update((s) => ({ ...s, user: null }));
			closeSSE();
			sseStarted = false;
			if ($authSession.enabled) {
				await goto('/login');
			}
		}
	}

	$: visibleNavItems = navItems.filter((item) => (item.href === '/review' ? $canReview : true));
</script>

{#if currentPath === '/login'}
	<slot />
{:else}
<AppShell>
	<svelte:fragment slot="sidebarLeft">
		<AppRail>
			<!-- Logo/Brand -->
			<svelte:fragment slot="lead">
				<AppRailAnchor href="/" class="lg:aspect-auto">
					<div class="flex flex-col items-center gap-1 py-2">
						<svg
							xmlns="http://www.w3.org/2000/svg"
							class="h-8 w-8"
							fill="none"
							viewBox="0 0 24 24"
							stroke="currentColor"
						>
							<path
								stroke-linecap="round"
								stroke-linejoin="round"
								stroke-width="2"
								d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
							/>
						</svg>
						<span class="text-xs font-bold">SocTalk</span>
					</div>
				</AppRailAnchor>
			</svelte:fragment>

			<!-- Navigation Items -->
			{#each visibleNavItems as item}
				<AppRailAnchor
					href={item.href}
					selected={currentPath === item.href || (item.href !== '/' && currentPath.startsWith(item.href))}
				>
					<svelte:fragment slot="lead">
						<div class="relative">
							<svg
								xmlns="http://www.w3.org/2000/svg"
								class="h-6 w-6"
								fill="none"
								viewBox="0 0 24 24"
								stroke="currentColor"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="2"
									d={item.icon}
								/>
							</svg>
							{#if item.badge && $pendingReviewsCount > 0}
								<span class="badge-icon variant-filled-warning absolute -top-1 -right-1 text-xs">
									{$pendingReviewsCount}
								</span>
							{/if}
						</div>
					</svelte:fragment>
					<span class="text-xs">{item.label}</span>
				</AppRailAnchor>
			{/each}

			<!-- SSE Status Indicator -->
			<svelte:fragment slot="trail">
				<div class="p-2 flex flex-col items-center gap-1">
					<div
						class="w-3 h-3 rounded-full {$sseStatus.connected
							? 'bg-green-500 status-indicator-active'
							: 'bg-red-500 status-indicator-error'}"
						title={$sseStatus.connected ? 'Connected' : $sseStatus.error || 'Disconnected'}
					></div>
					<span class="text-xs opacity-60">
						{$sseStatus.connected ? 'Live' : 'Offline'}
					</span>
				</div>
			</svelte:fragment>
		</AppRail>
	</svelte:fragment>

	<svelte:fragment slot="header">
		<AppBar class="border-b border-surface-500/30">
			<svelte:fragment slot="lead">
				<div class="h4">
					{#if currentPath === '/'}
						Dashboard
					{:else if currentPath.startsWith('/investigations')}
						Investigations
					{:else if currentPath.startsWith('/review')}
						Human Review
					{:else if currentPath.startsWith('/analytics')}
						Analytics
					{:else if currentPath.startsWith('/audit')}
						Audit Log
					{:else if currentPath.startsWith('/settings')}
						Settings
					{/if}
				</div>
			</svelte:fragment>
			<svelte:fragment slot="trail">
				<div class="flex items-center gap-3">
					{#if $authSession.enabled && $authSession.user}
						<span class="text-sm opacity-70">{$authSession.user.username}</span>
						<button type="button" class="btn btn-sm variant-ghost-surface" on:click={logout}>
							Log out
						</button>
					{:else}
						<span class="text-sm opacity-60">Security Operations Control Plane</span>
					{/if}
				</div>
			</svelte:fragment>
		</AppBar>
	</svelte:fragment>

	<!-- Page Content -->
	<div class="container mx-auto p-4">
		<slot />
	</div>
</AppShell>

<!-- Toast Notifications -->
<Toast />
{/if}
