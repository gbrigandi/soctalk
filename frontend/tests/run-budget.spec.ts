/**
 * #103 — per-tenant Agent Run token budget UI.
 *
 * MSSP tenant-detail page: the editable budget card loads the effective budget,
 * lets an admin set/clear the override (validated against the install cap), and
 * reflects the resolved effective value. Backend is mocked; a stateful
 * run-budget route echoes PATCHes so save/clear are observable.
 */
import { expect, test } from '@playwright/test';
import { mockAuthMe } from './helpers';

const TID = '11111111-1111-4111-8111-111111111111';
const INSTALL_DEFAULT = 200000;
const INSTALL_MAX = 500000;

function view(override: number | null) {
	const effective = Math.min(override ?? INSTALL_DEFAULT, INSTALL_MAX);
	return {
		install_default: INSTALL_DEFAULT,
		install_max: INSTALL_MAX,
		tenant_override: override,
		effective,
		spend_24h_tokens: 12345
	};
}

test.describe('Run budget (#103)', () => {
	test('MSSP can view, set, and clear the per-tenant override', async ({ page }) => {
		await mockAuthMe(page);
		let override: number | null = null; // server-side state

		await page.route(`**/api/mssp/tenants/${TID}`, (r) =>
			r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					id: TID,
					slug: 'acme',
					display_name: 'Acme Corp',
					state: 'active',
					profile: null,
					created_at: new Date().toISOString(),
					state_changed_at: new Date().toISOString()
				})
			})
		);
		await page.route(`**/api/mssp/tenants/${TID}/events*`, (r) =>
			r.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
		);
		await page.route(`**/api/mssp/tenants/${TID}/llm`, (r) =>
			r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'openai-compatible',
					base_url: 'https://api.openai.com/v1',
					model: 'gpt-4o',
					temperature: 0,
					max_tokens: 4096,
					dollar_budget_per_run: null,
					has_api_key: true,
					api_key_preview: 'sk-…abcd',
					tiers: null
				})
			})
		);
		await page.route(`**/api/mssp/tenants/${TID}/run-budget`, async (r) => {
			if (r.request().method() === 'PATCH') {
				const body = JSON.parse(r.request().postData() || '{}');
				override = body.override; // set or clear
			}
			await r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(view(override))
			});
		});

		await page.goto(`/tenants/${TID}`);

		const panel = page.getByTestId('run-budget-panel');
		await expect(panel).toBeVisible({ timeout: 15000 });
		await expect(page.getByTestId('run-budget-default')).toHaveText('200,000');
		await expect(page.getByTestId('run-budget-cap')).toHaveText('500,000');
		await expect(page.getByTestId('run-budget-override')).toHaveText('—');
		await expect(page.getByTestId('run-budget-effective')).toContainText('200,000');
		await expect(page.getByTestId('run-budget-spend')).toHaveText('12,345');

		// Client-side validation: above the install cap is rejected, no PATCH.
		await page.getByTestId('run-budget-input').fill('600000');
		await page.getByTestId('run-budget-save').click();
		await expect(page.getByTestId('run-budget-error')).toContainText(/install cap/i);
		await expect(page.getByTestId('run-budget-override')).toHaveText('—');

		// Set a valid override -> effective + override reflect it.
		await page.getByTestId('run-budget-input').fill('40000');
		await page.getByTestId('run-budget-save').click();
		await expect(page.getByTestId('run-budget-override')).toHaveText('40,000');
		await expect(page.getByTestId('run-budget-effective')).toContainText('40,000');

		// Clear -> reverts to the install default.
		await page.getByTestId('run-budget-clear').click();
		await expect(page.getByTestId('run-budget-override')).toHaveText('—');
		await expect(page.getByTestId('run-budget-effective')).toContainText('200,000');
	});
});
