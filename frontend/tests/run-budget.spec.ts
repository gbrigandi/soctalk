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

const DOLLAR_DEFAULT = 5;
const DOLLAR_MAX = 1000;

/** The full RunBudgetView the panel now reads (#128 dollars, #129 24h). */
function view(override: number | null, dollarOverride: number | null = null) {
	const effective = Math.min(override ?? INSTALL_DEFAULT, INSTALL_MAX);
	return {
		install_default: INSTALL_DEFAULT,
		install_max: INSTALL_MAX,
		tenant_override: override,
		effective,
		spend_today_tokens: 12345,
		dollar_install_default: DOLLAR_DEFAULT,
		dollar_install_max: DOLLAR_MAX,
		dollar_tenant_override: dollarOverride,
		dollar_effective: Math.min(dollarOverride ?? DOLLAR_DEFAULT, DOLLAR_MAX),
		spend_today_dollars: 1.25,
		daily_token_cap: 10_000_000,
		daily_dollar_cap: 50,
		daily_tokens_remaining: 9_987_655,
		daily_dollars_remaining: 48.75,
		daily_cap_hit: false,
		daily_cap_reason: null,
		daily_token_install_default: 10_000_000,
		daily_dollar_install_default: 50,
		daily_token_max: 10_000_000_000,
		daily_dollar_max: 100_000,
		daily_token_override: null,
		daily_dollar_override: null
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
				// The panel sends only CHANGED fields, naming each dimension.
				if ('token_override' in body) override = body.token_override;
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
		await expect(page.getByTestId('run-budget-default')).toContainText('200,000');
		await expect(page.getByTestId('run-budget-cap')).toContainText('500,000');
		await expect(page.getByTestId('run-budget-override')).toContainText('—');
		await expect(page.getByTestId('run-budget-effective')).toContainText('200,000');
		await expect(page.getByTestId('run-budget-spend')).toContainText('12,345');
		// Both dimensions and the rolling window are on screen (#128, #129).
		await expect(page.getByTestId('run-budget-dollar-effective')).toContainText('$5.00');
		await expect(page.getByTestId('run-budget-daily-cap')).toContainText('$50.00');
		await expect(page.getByTestId('run-budget-daily-remaining')).toContainText('$48.75');

		// Client-side validation: above the install cap is rejected, no PATCH.
		await page.getByTestId('run-budget-input').fill('600000');
		await page.getByTestId('run-budget-save').click();
		await expect(page.getByTestId('run-budget-error')).toContainText(/install cap/i);
		await expect(page.getByTestId('run-budget-override')).toContainText('—');

		// Set a valid override -> effective + override reflect it.
		await page.getByTestId('run-budget-input').fill('40000');
		await page.getByTestId('run-budget-save').click();
		await expect(page.getByTestId('run-budget-override')).toContainText('40,000');
		await expect(page.getByTestId('run-budget-effective')).toContainText('40,000');

		// Clear -> reverts to the install default.
		await page.getByTestId('run-budget-clear').click();
		await expect(page.getByTestId('run-budget-override')).toContainText('—');
		await expect(page.getByTestId('run-budget-effective')).toContainText('200,000');
	});
});
