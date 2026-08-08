import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const TID = '11111111-1111-1111-1111-111111111111';
const PERMS = ['view_authorization_facts', 'manage_authorization_facts', 'view_tenants', 'view_dashboard'];

async function wire(page: Page, currentTenant: string | null) {
	await page.route('**/auth/me', (r) =>
		r.fulfill({
			status: 200, contentType: 'application/json', body: JSON.stringify({
				user_id: 'u1', email: 'admin@mssp.example', user_type: 'mssp', role: 'mssp_admin',
				tenant_id: null, current_tenant: currentTenant,
				current_tenant_slug: currentTenant ? 'acme' : null, permissions: PERMS
			})
		})
	);
	await page.route('**/api/mssp/tenants/*/authorization/facts', (r) =>
		r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ facts: [] }) })
	);
}

// Steady state, no tenant pinned on the session: what does the operator actually see?
test('no tenant selected: button state + what the page says', async ({ page }) => {
	const errors: string[] = [];
	page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

	await wire(page, null);
	await page.goto((process.env.BASE ?? '') + '/authorization');
	const btn = page.getByRole('button', { name: '+ New fact' });
	await btn.waitFor({ state: 'attached' });
	await page.waitForTimeout(1200); // let auth/me hydrate the stores

	console.log('RENDERED   =', await btn.count());
	console.log('VISIBLE    =', await btn.isVisible());
	console.log('DISABLED   =', await btn.isDisabled());
	console.log('TOOLTIP    =', JSON.stringify(await btn.getAttribute('title')));
	console.log('PAGE SAYS  =', (await page.locator('.p-6 p.text-gray-400').last().textContent())?.trim());

	await btn.click({ force: true }).catch((e) => console.log('click threw:', e.message.split('\n')[0]));
	await page.waitForTimeout(400);
	console.log('DIALOG OPENED =', await page.getByRole('heading', { name: /New authorization fact/i }).isVisible());
	console.log('PAGE ERRORS   =', errors.length ? errors : 'none');
	await page.screenshot({ path: 'test-results/no-tenant-state.png' });
});
