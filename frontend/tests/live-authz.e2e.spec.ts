import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

/**
 * Live E2E against the deployed demo: authorization facts (all four kinds) +
 * engagements, through the real UI, real API, real DB. No mocks.
 *
 * Gated off the default suite. Run with:
 *   SMOKE_BASE_URL=https://demo.soctalk.ai \
 *   SMOKE_ADMIN_EMAIL=... SMOKE_ADMIN_PW=... \
 *   npx playwright test tests/live-authz.e2e.spec.ts
 *
 * Everything created is revoked afterwards (revoke is a soft delete server-side,
 * so audit rows survive, but the UI lists stay clean).
 */
const BASE = process.env.SMOKE_BASE_URL ?? '';
const EMAIL = process.env.SMOKE_ADMIN_EMAIL ?? '';
const PW = process.env.SMOKE_ADMIN_PW ?? '';

test.skip(!BASE || !EMAIL || !PW, 'live-only: needs SMOKE_BASE_URL/_ADMIN_EMAIL/_ADMIN_PW');
test.describe.configure({ mode: 'serial' });
test.setTimeout(120_000);

const RUN = `e2e${Date.now().toString(36)}`;

async function login(page: Page) {
	await page.goto(`${BASE}/login`);
	await page.locator('input[type=email]').fill(EMAIL);
	await page.locator('input[type=password]').fill(PW);
	await page.getByRole('button', { name: /sign in/i }).click();
	await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 30_000 });
}

async function pinTenant(page: Page) {
	// The header chip shows either "All tenants" (unpinned) or "Tenant: <name>".
	// The ops session on demo is typically already pinned — only pin if not.
	await expect(page.getByText(/All tenants|Tenant: /).first()).toBeVisible({ timeout: 30_000 });
	if ((await page.getByText('All tenants').count()) === 0) return;
	await page.goto(`${BASE}/tenants`);
	const open = page.getByRole('button', { name: 'Open SOC' }).and(page.locator(':not([disabled])'));
	await expect(open.first()).toBeVisible({ timeout: 30_000 });
	await open.first().click();
	await expect(page.getByText('All tenants')).toHaveCount(0, { timeout: 15_000 });
}

async function openNewFact(page: Page) {
	await page.goto(`${BASE}/authorization`);
	const btn = page.getByRole('button', { name: '+ New fact' });
	await expect(btn).toBeEnabled({ timeout: 30_000 });
	await btn.click();
	await expect(page.getByRole('heading', { name: /New authorization fact/i })).toBeVisible();
}

async function submitFactAndExpectRow(page: Page, id: string) {
	await page.getByRole('button', { name: 'Create fact' }).click();
	// Modal closes on success and the list reloads with the new row.
	await expect(page.getByRole('heading', { name: /New authorization fact/i })).toHaveCount(0, {
		timeout: 30_000
	});
	await expect(page.getByText(id).first()).toBeVisible({ timeout: 30_000 });
}

async function revokeRow(page: Page, id: string) {
	page.once('dialog', (d) => d.accept('e2e cleanup'));
	const row = page.locator('tr', { hasText: id });
	await row.getByRole('button', { name: 'Revoke' }).click();
	await expect(page.getByText(id)).toHaveCount(0, { timeout: 30_000 });
}

test('login and pin a tenant', async ({ page }) => {
	await login(page);
	await pinTenant(page);
});

test('GRANT (change_ticket) fact: create, listed, revoke', async ({ page }) => {
	await login(page);
	await pinTenant(page);
	await openNewFact(page);
	const id = `${RUN}-GRANT`;
	// grant + account track are the defaults.
	await page.getByPlaceholder('CHG-1001', { exact: true }).fill(id);
	await page.getByPlaceholder('svc-deploy').fill('svc-deploy');
	await page.getByPlaceholder('db-01').fill('db-01');
	await page.getByPlaceholder('sudo-exec').fill('sudo-exec');
	await page.locator('input[type=date]').nth(1).fill('2026-12-31'); // valid_until: required for change_ticket
	await submitFactAndExpectRow(page, id);
	await revokeRow(page, id);
});

test('PROHIBITION fact: create, listed, revoke', async ({ page }) => {
	await login(page);
	await pinTenant(page);
	await openNewFact(page);
	const id = `${RUN}-PROHIB`;
	await page.getByRole('button', { name: /Prohibition/ }).click();
	await page.getByPlaceholder('CHG-1001', { exact: true }).fill(id);
	await page.getByPlaceholder('svc-deploy').fill('svc-backup');
	await page.getByPlaceholder('db-01').fill('db-01');
	await page.getByPlaceholder('interactive-shell').fill('interactive-shell');
	await submitFactAndExpectRow(page, id);
	await revokeRow(page, id);
});

test('CHANGE FREEZE fact: create, listed, revoke', async ({ page }) => {
	await login(page);
	await pinTenant(page);
	await openNewFact(page);
	const id = `${RUN}-FREEZE`;
	await page.getByRole('button', { name: /Change freeze/ }).click();
	await page.getByPlaceholder('CHG-1001', { exact: true }).fill(id);
	await page.locator('input[type=datetime-local]').first().fill('2026-12-24T00:00');
	await page.locator('input[type=datetime-local]').last().fill('2026-12-26T23:59');
	await page.getByPlaceholder('prod').fill('prod');
	await submitFactAndExpectRow(page, id);
	await revokeRow(page, id);
});

test('ENTITY CONTEXT fact: create, listed, revoke', async ({ page }) => {
	await login(page);
	await pinTenant(page);
	await openNewFact(page);
	const id = `${RUN}-ENTITY`;
	await page.getByRole('button', { name: /Entity context/ }).click();
	await page.getByPlaceholder('CHG-1001', { exact: true }).fill(id);
	// entity_type=asset is the default; name is the authoritative key.
	await page.getByPlaceholder('db-01').last().fill('db-01');
	await page.getByPlaceholder('prod').first().fill('prod');
	await submitFactAndExpectRow(page, id);
	await revokeRow(page, id);
});

test('ENGAGEMENT: declare, listed with status, revoke', async ({ page }) => {
	await login(page);
	await pinTenant(page);
	await page.goto(`${BASE}/authorization?tab=engagements`);
	const declareBtn = page.getByRole('button', { name: 'Declare engagement' });
	await expect(declareBtn).toBeEnabled({ timeout: 30_000 });
	await declareBtn.click();

	const name = `${RUN} pentest window`;
	await page.getByPlaceholder('Q3 external pentest').fill(name);
	await page.locator('input[type=datetime-local]').first().fill('2026-08-01T09:00');
	await page.locator('input[type=datetime-local]').last().fill('2026-08-05T18:00');
	await page.getByPlaceholder('203.0.113.0/24').fill('203.0.113.0/24');
	await page.getByPlaceholder('web-01, db-01').fill('web-01');
	await page.getByPlaceholder('T1078, T1110.001').fill('T1078');
	await page.getByRole('button', { name: 'Declare', exact: true }).click();

	const card = page.locator('.card', { hasText: name });
	await expect(card).toBeVisible({ timeout: 30_000 });
	await expect(card.getByText(/scheduled|active/)).toBeVisible();

	// Revoke (window.prompt) and expect the revoked badge on the same card.
	page.once('dialog', (d) => d.accept('e2e cleanup'));
	await card.getByRole('button', { name: 'Revoke' }).click();
	await expect(card.getByText('revoked')).toBeVisible({ timeout: 30_000 });
});

// Safety net: earlier failed runs may have left facts behind (a fact is created
// before the assertion that failed). Sweep anything with the e2e id prefix so
// the demo tenant never accumulates test residue.
test('CLEANUP: sweep stray e2e facts from prior runs', async ({ page }) => {
	await login(page);
	await pinTenant(page);
	await page.goto(`${BASE}/authorization`);
	await page.getByRole('button', { name: '+ New fact' }).waitFor({ timeout: 30_000 });
	for (let i = 0; i < 20; i++) {
		const row = page.locator('tr', { hasText: /e2e[a-z0-9]+-(GRANT|PROHIB|FREEZE|ENTITY)/ }).first();
		if ((await row.count()) === 0) break;
		page.once('dialog', (d) => d.accept('e2e cleanup'));
		await row.getByRole('button', { name: 'Revoke' }).click();
		await page.waitForTimeout(1500);
	}
	await expect(page.locator('tr', { hasText: /e2e[a-z0-9]+-(GRANT|PROHIB|FREEZE|ENTITY)/ })).toHaveCount(0);
});
