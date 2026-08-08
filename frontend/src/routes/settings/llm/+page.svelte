<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type TenantLlmConfig } from '$lib/api/client';
	import RunBudgetPanel from '$lib/components/tenants/RunBudgetPanel.svelte';
	import { addToast, authSession, isMsspUser } from '$lib/stores';
	import { goto } from '$app/navigation';
	import { m } from '$lib/paraglide/messages';
	import { localizeHref, localizedGoto } from '$lib/i18n';

	// Tenant-side BYOK page. Tenant_admins paste their own LLM API
	// key here so the runs-worker (which executes investigation
	// graphs FOR this tenant only) consumes the tenant's vendor
	// credential instead of the MSSP's shared install key.
	//
	// Provider + base URL are MSSP-controlled and shown read-only —
	// the install's outbound egress allowlist is provider-pinned, so
	// letting the tenant flip Anthropic→OpenAI from this form would
	// either DoS their own runs (egress blocked) or require us to
	// re-render Cilium policy at request time. Both worse than
	// "tenant supplies a credential within the MSSP's chosen
	// provider".

	let cfg: TenantLlmConfig | null = null;
	let loading = true;
	let saving = false;
	let clearing = false;
	let error: string | null = null;
	let pasted = '';

	onMount(async () => {
		try {
			cfg = await api.tenantLlm.get();
		} catch (e) {
			error = e instanceof Error ? e.message : m.llm_load_failed();
		} finally {
			loading = false;
		}
	});

	// This is a tenant-side (/api/tenant/*) page. Any MSSP-side user who
	// reaches it by URL — whether at MSSP-wide scope OR pinned into a
	// tenant — is denied by the audience wall, so bounce them instead of
	// letting the tenant-LLM fetch surface a raw "MSSP-side user denied"
	// error. Letting a tenant-pinned MSSP operator manage this on the
	// tenant's behalf (the Open-SOC wholesale flow) needs backend support
	// for impersonated /api/tenant/* access and is tracked separately.
	$: if ($authSession.user && $isMsspUser) {
		localizedGoto('/tenants');
	}

	async function setKey() {
		if (!pasted.trim()) {
			error = m.llm_key_empty();
			return;
		}
		saving = true;
		error = null;
		try {
			cfg = await api.tenantLlm.setKey(pasted.trim());
			pasted = '';
			addToast({
				type: 'success',
				title: m.llm_key_updated_title(),
				message: m.llm_key_updated_msg(),
			});
		} catch (e) {
			error = e instanceof Error ? e.message : m.llm_key_update_failed();
		} finally {
			saving = false;
		}
	}

	async function clearKey() {
		clearing = true;
		error = null;
		try {
			cfg = await api.tenantLlm.clearKey();
			addToast({
				type: 'success',
				title: m.llm_reverted_title(),
				message: m.llm_reverted_msg(),
			});
		} catch (e) {
			error = e instanceof Error ? e.message : m.llm_key_clear_failed();
		} finally {
			clearing = false;
		}
	}
</script>

<svelte:head>
	<title>{m.llm_page_title()} — SocTalk</title>
</svelte:head>

<div class="space-y-6 max-w-3xl">
	<header class="flex items-baseline justify-between">
		<h1 class="h2">{m.llm_heading()}</h1>
		<a href={localizeHref('/settings')} class="anchor text-sm">{m.llm_all_settings()}</a>
	</header>

	<p class="opacity-80 text-sm">{m.llm_intro()}</p>

	{#if loading}
		<div class="card p-6 flex items-center gap-3">
			<span
				class="inline-block animate-spin rounded-full h-4 w-4 border-b-2 border-current"
			></span>
			<span>{m.common_loading()}</span>
		</div>
	{:else if !cfg}
		<div class="card p-6 text-error-500">
			{error ?? m.llm_load_failed_cfg()}
		</div>
	{:else}
		<div class="card p-6 space-y-5">
			<div>
				<h3 class="h4">{m.llm_provider()}</h3>
				<dl class="grid grid-cols-3 gap-2 text-sm mt-2">
					<dt class="opacity-60">{m.llm_vendor()}</dt>
					<dd class="col-span-2">
						<code class="text-xs">{cfg.provider}</code>
					</dd>
					<dt class="opacity-60">{m.llm_endpoint()}</dt>
					<dd class="col-span-2">
						<code class="text-xs">{cfg.base_url}</code>
					</dd>
					<dt class="opacity-60">{m.llm_model()}</dt>
					<dd class="col-span-2">
						<code class="text-xs">{cfg.model}</code>
					</dd>
				</dl>
				<p class="text-xs opacity-60 mt-2">{m.llm_provider_hint()}</p>
			</div>

			<hr class="opacity-30" />

			<div>
				<h3 class="h4">{m.llm_api_key()}</h3>
				{#if cfg.has_api_key}
					<div
						class="mt-2 flex items-center gap-3 p-3 rounded
                          bg-success-500/10 border border-success-500/30"
					>
						<span class="badge variant-soft-success">{m.llm_byok_active()}</span>
						<code class="text-xs">{cfg.api_key_preview}</code>
						<span class="text-xs opacity-70 ml-auto">{m.llm_runs_use_your_key()}</span>
					</div>
				{:else}
					<div
						class="mt-2 flex items-center gap-3 p-3 rounded
                          bg-warning-500/10 border border-warning-500/30"
					>
						<span class="badge variant-soft-warning">{m.llm_mssp_shared()}</span>
						<span class="text-xs">{m.llm_shared_in_use()}</span>
					</div>
				{/if}

				<form
					on:submit|preventDefault={setKey}
					class="mt-4 space-y-3"
				>
					<label class="block">
						<span class="text-sm opacity-80">
							{cfg.has_api_key ? m.llm_replace_key() : m.llm_new_key()}
						</span>
						<input
							type="password"
							class="input mt-1"
							placeholder="sk-ant-… or sk-…"
							bind:value={pasted}
							autocomplete="off"
							disabled={saving || clearing}
						/>
					</label>
					<p class="text-xs opacity-60">{m.llm_storage_hint()}</p>
					{#if error}
						<p class="text-error-500 text-sm">{error}</p>
					{/if}
					<div class="flex items-center gap-2">
						<button
							type="submit"
							class="btn variant-filled-primary"
							disabled={saving || clearing || !pasted.trim()}
						>
							{saving ? m.common_saving() : cfg.has_api_key ? m.llm_replace_key() : m.llm_save_key()}
						</button>
						{#if cfg.has_api_key}
							<button
								type="button"
								class="btn variant-ghost-surface"
								on:click={clearKey}
								disabled={saving || clearing}
								title={m.llm_revert_title_attr()}
							>
								{clearing ? m.llm_reverting() : m.llm_revert()}
							</button>
						{/if}
					</div>
				</form>
			</div>
		</div>
		<!-- Read-only effective run budget (#103): tenants see it, can't raise it. -->
		<div class="mt-4">
			<RunBudgetPanel />
		</div>
	{/if}
</div>
