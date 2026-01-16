<script lang="ts">
	import { toasts, removeToast, type Toast } from '$lib/stores';
	import { fade, fly } from 'svelte/transition';

	function getToastClass(type: Toast['type']): string {
		switch (type) {
			case 'success':
				return 'variant-filled-success';
			case 'warning':
				return 'variant-filled-warning';
			case 'error':
				return 'variant-filled-error';
			default:
				return 'variant-filled-primary';
		}
	}

	function getIcon(type: Toast['type']): string {
		switch (type) {
			case 'success':
				return 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z';
			case 'warning':
				return 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z';
			case 'error':
				return 'M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z';
			default:
				return 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z';
		}
	}
</script>

<div class="toast-container">
	{#each $toasts as toast (toast.id)}
		<div
			class="card p-4 shadow-lg {getToastClass(toast.type)} max-w-sm"
			in:fly={{ x: 300, duration: 300 }}
			out:fade={{ duration: 200 }}
		>
			<div class="flex items-start gap-3">
				<svg
					xmlns="http://www.w3.org/2000/svg"
					class="h-6 w-6 flex-shrink-0"
					fill="none"
					viewBox="0 0 24 24"
					stroke="currentColor"
				>
					<path
						stroke-linecap="round"
						stroke-linejoin="round"
						stroke-width="2"
						d={getIcon(toast.type)}
					/>
				</svg>
				<div class="flex-1">
					{#if toast.title}
						<h4 class="font-bold">{toast.title}</h4>
					{/if}
					<p class="text-sm">{toast.message}</p>
				</div>
				<button
					class="btn-icon btn-icon-sm variant-soft"
					on:click={() => removeToast(toast.id)}
					aria-label="Dismiss"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						class="h-4 w-4"
						fill="none"
						viewBox="0 0 24 24"
						stroke="currentColor"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M6 18L18 6M6 6l12 12"
						/>
					</svg>
				</button>
			</div>
		</div>
	{/each}
</div>
