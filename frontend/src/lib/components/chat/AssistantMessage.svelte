<script lang="ts">
	import Markdown from '$lib/components/Markdown.svelte';

	export let text: string;
	export let timestamp: string | null = null;
	export let streaming = false;
</script>

<div class="flex flex-col items-start">
	<div
		class="bg-surface-500/15 rounded-lg px-3 py-2 max-w-[95%] text-sm"
		data-testid="assistant-message"
	>
		<!-- Rendered as sanitized GFM (#98): the model emits tables/bold/headings
		     and tool answers are unreadable as raw pipes. Markdown re-renders on
		     every streaming delta; partial input stays safe (an unclosed fence
		     renders as a code block until its close streams in). The caret is a
		     sibling of the rendered block, never inside it. -->
		<Markdown {text} />
		{#if streaming}
			<span class="inline-block w-2 h-3 bg-primary-500 align-middle ml-1 animate-pulse"></span>
		{/if}
	</div>
	{#if timestamp}
		<span class="text-xs opacity-40 mt-1">
			{new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
		</span>
	{/if}
</div>
