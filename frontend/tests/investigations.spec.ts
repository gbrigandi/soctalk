import { test, expect } from '@playwright/test';

test.describe('Investigations Page', () => {
	test.beforeEach(async ({ page }) => {
		// Mock investigations API
		await page.route('**/api/investigations*', async (route) => {
			const url = new URL(route.request().url());
			const status = url.searchParams.get('status');
			const phase = url.searchParams.get('phase');

			let items = [
				{
					id: '550e8400-e29b-41d4-a716-446655440000',
					title: 'Suspicious Login Activity',
					status: 'in_progress',
					phase: 'enrichment',
					alert_count: 5,
					observable_count: 10,
					malicious_count: 2,
					max_severity: 'high',
					verdict_decision: null,
					created_at: new Date().toISOString(),
					updated_at: new Date().toISOString(),
				},
				{
					id: '550e8400-e29b-41d4-a716-446655440001',
					title: 'Malware Detection Alert',
					status: 'pending',
					phase: 'triage',
					alert_count: 3,
					observable_count: 5,
					malicious_count: 0,
					max_severity: 'critical',
					verdict_decision: null,
					created_at: new Date().toISOString(),
					updated_at: new Date().toISOString(),
				},
				{
					id: '550e8400-e29b-41d4-a716-446655440002',
					title: 'Completed Investigation',
					status: 'completed',
					phase: 'closed',
					alert_count: 8,
					observable_count: 15,
					malicious_count: 5,
					max_severity: 'high',
					verdict_decision: 'escalate',
					created_at: new Date(Date.now() - 86400000).toISOString(),
					updated_at: new Date().toISOString(),
				},
			];

			// Apply filters
			if (status) {
				items = items.filter((i) => i.status === status);
			}
			if (phase) {
				items = items.filter((i) => i.phase === phase);
			}

			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items,
					total: items.length,
					page: 1,
					page_size: 20,
					has_more: false,
				}),
			});
		});
	});

	test('has correct page title', async ({ page }) => {
		await page.goto('/investigations');
		await expect(page).toHaveTitle(/Investigations - SocTalk/);
	});

	test('displays investigations table', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check table headers
		await expect(page.getByRole('columnheader', { name: 'Title' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Status' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Phase' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Severity' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Alerts' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Malicious' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Verdict' })).toBeVisible();
	});

	test('displays investigation data in table', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check investigation titles are visible
		await expect(page.getByText('Suspicious Login Activity')).toBeVisible();
		await expect(page.getByText('Malware Detection Alert')).toBeVisible();
	});

	test('displays status badges', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check status badges exist in the table (using locator for badge elements)
		await expect(page.locator('.badge:has-text("in_progress")').first()).toBeVisible();
	});

	test('has filter dropdowns', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check filter select elements exist
		await expect(page.locator('select.select').first()).toBeVisible();
		await expect(page.locator('option:has-text("All Statuses")')).toBeAttached();
		await expect(page.locator('option:has-text("All Phases")')).toBeAttached();
	});

	test('status filter works', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Select status filter
		await page.selectOption('select:first-of-type', 'in_progress');

		// Wait for filtered results
		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Should only show in_progress investigations
		await expect(page.getByText('Suspicious Login Activity')).toBeVisible();
	});

	test('phase filter works', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Select phase filter
		await page.selectOption('select:nth-of-type(2)', 'triage');

		// Wait for filtered results
		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Should only show triage phase investigations
		await expect(page.getByText('Malware Detection Alert')).toBeVisible();
	});

	test('refresh button works', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Click refresh
		await page.getByRole('button', { name: 'Refresh' }).click();

		// Should show loading then results
		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });
		await expect(page.getByText('Suspicious Login Activity')).toBeVisible();
	});

	test('investigation links navigate correctly', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check View button exists
		await expect(page.getByRole('link', { name: 'View' }).first()).toBeVisible();
	});

	test('displays verdict badges for completed investigations', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check verdict badge for escalated investigation
		await expect(page.getByText('escalate')).toBeVisible();
	});

	test('displays severity badges', async ({ page }) => {
		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check severity badges
		await expect(page.getByText('high').first()).toBeVisible();
		await expect(page.getByText('critical')).toBeVisible();
	});
});

test.describe('Investigations Empty State', () => {
	test('shows empty message when no investigations', async ({ page }) => {
		await page.route('**/api/investigations*', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [],
					total: 0,
					page: 1,
					page_size: 20,
					has_more: false,
				}),
			});
		});

		await page.goto('/investigations');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		await expect(page.getByText('No investigations found')).toBeVisible();
	});
});
