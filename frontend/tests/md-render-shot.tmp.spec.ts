/** Throwaway: screenshot a real rendered assistant markdown response (#98). */
import { test } from '@playwright/test';
import { mockAuthMe } from './helpers';

const CONV_ID = 'c0ffee00-0000-4000-8000-0000000000aa';

const ANSWER = [
	"Here's the fleet picture for tenants with **open critical** investigations:",
	'',
	'| Tenant | Slug | Open | Max Severity | Oldest |',
	'|---|---|---|---|---|',
	'| **Northwind Labs** | `northwind` | 8 | **13** | 4h 12m |',
	'| Demo Tenant | `demo` | 124 | 12 | 22h 03m |',
	'| Acme Corp | `acme` | 3 | 10 | 1h 47m |',
	'',
	'## What stands out',
	'',
	'- `northwind` has the highest severity (**13**) and its oldest case has sat for over 4 hours.',
	'- `demo` carries the largest backlog but nothing above severity 12.',
	'',
	'> Recommend triaging `northwind`\'s severity-13 case first — it is past the 2-hour SLA for criticals.',
	'',
	'The query behind this, if you want to reuse it:',
	'',
	'```sql',
	'SELECT slug, count(*) AS open, max(severity) AS max_sev',
	'FROM investigations',
	"WHERE status = 'active' AND severity >= 10",
	'GROUP BY slug ORDER BY max_sev DESC;',
	'```',
	'',
	'Full runbook: [Critical escalation SLA](https://soctalk.ai/runbook).'
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

test('shot: rendered assistant markdown', async ({ page }) => {
	test.setTimeout(60000);
	await page.setViewportSize({ width: 1000, height: 1100 });
	await mockAuthMe(page);
	await page.route('**/api/chat/conversations?*', (r) =>
		r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [conversation] }) })
	);
	await page.route(`**/api/chat/conversations/${CONV_ID}`, (r) =>
		r.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				conversation,
				messages: [
					{ id: 'u1', role: 'user', content: { text: 'Which tenants have open critical investigations?' }, created_at: new Date().toISOString() },
					{ id: 'm1', role: 'assistant', content: { text: ANSWER }, created_at: new Date().toISOString() }
				]
			})
		})
	);
	await page.goto('/chat');
	await page.getByText('Fleet check').first().click();
	await page.getByTestId('assistant-message').first().waitFor({ state: 'visible', timeout: 15000 });
	await page.locator('table').first().waitFor({ state: 'visible', timeout: 5000 });
	await page.waitForTimeout(400);
	await page.locator('.message-list').screenshot({ path: '/tmp/gpu-bench/md-rendered.png' });
});
