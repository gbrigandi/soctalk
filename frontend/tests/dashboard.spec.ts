import { test, expect } from '@playwright/test';

test.describe('Dashboard', () => {
	test.beforeEach(async ({ page }) => {
		// Mock API responses since backend may not be running
		await page.route('**/api/metrics/overview', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					open_investigations: 5,
					pending_reviews: 2,
					avg_time_to_triage_seconds: 300,
					avg_time_to_verdict_seconds: 1800,
					investigations_created_today: 10,
					investigations_closed_today: 8,
					escalations_today: 1,
					auto_closed_today: 4,
					malicious_observables_today: 3,
					verdict_breakdown: {
						auto_close: 4,
						escalate: 1,
						suspicious: 2,
					},
					severity_breakdown: {
						critical: 1,
						high: 2,
						medium: 2,
					},
				}),
			});
		});

		await page.route('**/api/metrics/hourly*', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					metrics: [
						{
							hour: new Date().toISOString(),
							investigations_created: 2,
							investigations_closed: 1,
							total_alerts: 5,
						},
					],
				}),
			});
		});

		await page.route('**/api/investigations*', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [
						{
							id: '550e8400-e29b-41d4-a716-446655440000',
							title: 'Test Investigation 1',
							status: 'in_progress',
							phase: 'enrichment',
							alert_count: 5,
							observable_count: 10,
							malicious_count: 2,
							max_severity: 'high',
							created_at: new Date().toISOString(),
							updated_at: new Date().toISOString(),
						},
						{
							id: '550e8400-e29b-41d4-a716-446655440001',
							title: 'Test Investigation 2',
							status: 'pending',
							phase: 'triage',
							alert_count: 3,
							observable_count: 5,
							malicious_count: 0,
							max_severity: 'medium',
							created_at: new Date().toISOString(),
							updated_at: new Date().toISOString(),
						},
					],
					total: 2,
					page: 1,
					page_size: 20,
					has_more: false,
				}),
			});
		});
	});

	test('has correct page title', async ({ page }) => {
		await page.goto('/');
		await expect(page).toHaveTitle(/Dashboard - SocTalk/);
	});

	test('displays KPI cards', async ({ page }) => {
		await page.goto('/');

		// Wait for loading to finish
		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check KPI cards are displayed
		await expect(page.getByText('Open Investigations')).toBeVisible();
		await expect(page.getByText('Pending Reviews')).toBeVisible();
		await expect(page.getByText('Avg. Time to Triage')).toBeVisible();
		await expect(page.getByText('Avg. Time to Verdict')).toBeVisible();
	});

	test('displays metrics values', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check that metric values are rendered
		await expect(page.getByText('5').first()).toBeVisible(); // Open investigations
	});

	test('displays today activity section', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check activity cards
		await expect(page.getByText('Created Today')).toBeVisible();
		await expect(page.getByText('Closed Today')).toBeVisible();
		await expect(page.getByText('Escalations')).toBeVisible();
		await expect(page.getByText('Auto-Closed')).toBeVisible();
		await expect(page.getByText('Malicious IOCs')).toBeVisible();
	});

	test('displays investigation throughput chart section', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		await expect(page.getByText('Investigation Throughput (24h)')).toBeVisible();
	});

	test('displays verdicts today section', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		await expect(page.getByText('Verdicts Today')).toBeVisible();
	});

	test('displays active investigations section', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		await expect(page.getByText('Active Investigations')).toBeVisible();
	});

	test('displays recent investigations table', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		await expect(page.getByText('Recent Investigations')).toBeVisible();

		// Check table headers
		await expect(page.getByRole('columnheader', { name: 'Title' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Status' })).toBeVisible();
		await expect(page.getByRole('columnheader', { name: 'Verdict' })).toBeVisible();
	});

	test('displays severity breakdown section', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		await expect(page.getByText('Open by Severity')).toBeVisible();
	});

	test('displays live event stream section', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		await expect(page.getByText('Live Event Stream')).toBeVisible();
	});

	test('investigation links are clickable', async ({ page }) => {
		await page.goto('/');

		await expect(page.locator('.animate-spin')).not.toBeVisible({ timeout: 10000 });

		// Check that View all link exists
		await expect(page.getByText('View all investigations')).toBeVisible();
	});
});

test.describe('Dashboard Error Handling', () => {
	test('shows error message when API fails', async ({ page }) => {
		// Mock API to return error
		await page.route('**/api/metrics/overview', async (route) => {
			await route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'Internal server error' }),
			});
		});

		await page.goto('/');

		// Should show error alert
		await expect(page.locator('.alert')).toBeVisible({ timeout: 10000 });
	});
});
