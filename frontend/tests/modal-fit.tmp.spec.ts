import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const TID = '11111111-1111-1111-1111-111111111111';
const MSSP_PERMS = [
	'view_authorization_facts', 'manage_authorization_facts', 'view_tenants', 'view_dashboard'
];

async function wire(page: Page) {
	await page.route('**/auth/me', (r) =>
		r.fulfill({
			status: 200, contentType: 'application/json', body: JSON.stringify({
				user_id: 'u1', email: 'admin@mssp.example', user_type: 'mssp', role: 'mssp_admin',
				tenant_id: null, current_tenant: TID, current_tenant_slug: 'acme', permissions: MSSP_PERMS
			})
		})
	);
	await page.route('**/api/mssp/tenants/*/authorization/facts', (r) =>
		r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ facts: [] }) })
	);
}

// The dialog is taller than a laptop viewport once "Entity context" is picked
// (it adds the whole ENTITY block). Assert nothing is clipped off the top and
// the submit button at the very bottom is reachable by scrolling.
test('new-fact dialog fits the canvas at a short viewport', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 720 });
	await wire(page);
	await page.goto('/authorization');
	await page.getByRole('button', { name: '+ New fact' }).click();
	await page.getByRole('button', { name: /Entity context/ }).click();

	const panel = page.locator('.card.max-w-2xl');
	const title = page.getByRole('heading', { name: /New authorization fact/i });

	const box = await panel.boundingBox();
	const vh = page.viewportSize()!.height;
	console.log(`panel top=${box!.y.toFixed(0)} height=${box!.height.toFixed(0)} viewport=${vh}`);

	// The regression: with centering on the scroll container the panel's top
	// went negative and could never be scrolled back into view.
	expect(box!.y).toBeGreaterThanOrEqual(0);
	await expect(title).toBeInViewport();

	// And the far end of the form must be reachable.
	const submit = page.getByRole('button', { name: 'Create fact' });
	await submit.scrollIntoViewIfNeeded();
	await expect(submit).toBeInViewport();

	// After scrolling to the bottom, the top must still be reachable.
	await title.scrollIntoViewIfNeeded();
	await expect(title).toBeInViewport();

	await page.screenshot({ path: 'test-results/modal-fit-top.png' });
});
