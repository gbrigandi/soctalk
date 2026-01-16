<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, type AuthSession } from '$lib/api/client';
	import { addToast, authSession } from '$lib/stores';

	let username = '';
	let password = '';
	let loading = true;
	let submitting = false;
	let mode: AuthSession['mode'] = 'none';
	let enabled = false;

	onMount(async () => {
		try {
			const session = await api.auth.session();
			authSession.set(session);
			enabled = session.enabled;
			mode = session.mode;

			if (!session.enabled || session.user) {
				await goto('/');
			}
		} catch (e) {
			addToast({
				type: 'error',
				title: 'Auth Error',
				message: e instanceof Error ? e.message : 'Unable to check session.'
			});
		} finally {
			loading = false;
		}
	});

	async function submit() {
		submitting = true;
		try {
			const res = await api.auth.login({ username, password });
			authSession.set({ enabled: true, mode: 'static', user: res.user });
			await goto('/');
		} catch (e) {
			addToast({
				type: 'error',
				title: 'Login Failed',
				message: e instanceof Error ? e.message : 'Invalid credentials.'
			});
		} finally {
			submitting = false;
		}
	}

	async function refresh() {
		loading = true;
		try {
			const session = await api.auth.session();
			authSession.set(session);
			enabled = session.enabled;
			mode = session.mode;
			if (!session.enabled || session.user) {
				await goto('/');
			}
		} finally {
			loading = false;
		}
	}
</script>

<div class="min-h-[calc(100vh-2rem)] flex items-center justify-center p-4">
	<div class="card max-w-md w-full p-6 space-y-4">
		<div class="space-y-1">
			<h1 class="h2">Sign in</h1>
			<p class="text-sm opacity-70">SocTalk Control Plane</p>
		</div>

		{#if loading}
			<div class="flex items-center gap-2 opacity-70">
				<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current"></span>
				<span>Loading…</span>
			</div>
		{:else if !enabled}
			<p class="opacity-70">Authentication is disabled for this deployment.</p>
			<button type="button" class="btn variant-filled-primary" on:click={() => goto('/')}>Continue</button>
		{:else if mode === 'proxy'}
			<p class="opacity-70">
				This deployment uses upstream authentication. Sign in via your SSO provider and refresh.
			</p>
			<button type="button" class="btn variant-filled-primary" on:click={refresh} disabled={loading}>
				Refresh Session
			</button>
		{:else}
			<form class="space-y-3" on:submit|preventDefault={submit}>
				<label class="label">
					<span class="font-medium">Username</span>
					<input class="input" autocomplete="username" bind:value={username} />
				</label>
				<label class="label">
					<span class="font-medium">Password</span>
					<input type="password" class="input" autocomplete="current-password" bind:value={password} />
				</label>
				<button type="submit" class="btn variant-filled-primary w-full" disabled={submitting}>
					{#if submitting}
						<span class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current mr-2"></span>
					{/if}
					Sign in
				</button>
			</form>
		{/if}
	</div>
</div>

