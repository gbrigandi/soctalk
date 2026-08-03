<!--
  Sanitized GFM renderer for model-generated text (#98).
  The {@html} below is safe ONLY because renderMarkdown() sanitizes with an
  explicit allowlist (see $lib/markdown.ts). Never bypass it.
-->
<script lang="ts">
	import { renderMarkdown } from '$lib/markdown';

	export let text: string;

	$: html = renderMarkdown(text);
</script>

<!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized in renderMarkdown -->
<div class="chat-md">{@html html}</div>

<style>
	.chat-md :global(p) {
		margin: 0.25rem 0;
	}
	.chat-md :global(p:first-child) {
		margin-top: 0;
	}
	.chat-md :global(p:last-child) {
		margin-bottom: 0;
	}
	.chat-md :global(h1),
	.chat-md :global(h2),
	.chat-md :global(h3),
	.chat-md :global(h4) {
		font-weight: 700;
		margin: 0.6rem 0 0.25rem;
		line-height: 1.3;
	}
	.chat-md :global(h1) {
		font-size: 1.1rem;
	}
	.chat-md :global(h2) {
		font-size: 1.05rem;
	}
	.chat-md :global(h3),
	.chat-md :global(h4) {
		font-size: 1rem;
	}
	.chat-md :global(ul),
	.chat-md :global(ol) {
		margin: 0.25rem 0;
		padding-left: 1.25rem;
	}
	.chat-md :global(ul) {
		list-style: disc;
	}
	.chat-md :global(ol) {
		list-style: decimal;
	}
	.chat-md :global(li) {
		margin: 0.125rem 0;
	}
	.chat-md :global(blockquote) {
		border-left: 3px solid rgb(var(--color-surface-400) / 0.5);
		padding-left: 0.75rem;
		margin: 0.375rem 0;
		opacity: 0.85;
	}
	.chat-md :global(code) {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.8125rem;
		background: rgb(var(--color-surface-500) / 0.25);
		border-radius: 0.25rem;
		padding: 0.0625rem 0.25rem;
	}
	.chat-md :global(pre) {
		background: rgb(var(--color-surface-500) / 0.25);
		border-radius: 0.375rem;
		padding: 0.5rem 0.75rem;
		margin: 0.375rem 0;
		overflow-x: auto;
	}
	.chat-md :global(pre code) {
		background: none;
		padding: 0;
	}
	/* Wide tool-call tables scroll inside the bubble instead of breaking it. */
	.chat-md :global(table) {
		display: block;
		max-width: 100%;
		overflow-x: auto;
		border-collapse: collapse;
		margin: 0.375rem 0;
		font-variant-numeric: tabular-nums;
	}
	.chat-md :global(th),
	.chat-md :global(td) {
		border: 1px solid rgb(var(--color-surface-400) / 0.4);
		padding: 0.25rem 0.5rem;
		text-align: left;
		white-space: nowrap;
	}
	.chat-md :global(th) {
		font-weight: 600;
		background: rgb(var(--color-surface-500) / 0.15);
	}
	.chat-md :global(a) {
		text-decoration: underline;
		color: rgb(var(--color-primary-400));
	}
	.chat-md :global(hr) {
		border: none;
		border-top: 1px solid rgb(var(--color-surface-400) / 0.4);
		margin: 0.5rem 0;
	}
</style>
