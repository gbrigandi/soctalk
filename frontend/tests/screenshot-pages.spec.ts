/**
 * Screenshot test - captures screenshots of all pages
 */
import { test } from '@playwright/test';

const FRONTEND_URL = 'http://localhost:5173';

test.describe('Page Screenshots', () => {
	test('capture all pages', async ({ page }) => {
		// Dashboard
		await page.goto(FRONTEND_URL);
		await page.waitForLoadState('domcontentloaded');
		await page.waitForTimeout(2000); // Wait for SSE and API calls
		await page.screenshot({ path: 'screenshots/01-dashboard.png', fullPage: true });

		// Investigations
		await page.goto(`${FRONTEND_URL}/investigations`);
		await page.waitForLoadState('domcontentloaded');
		await page.waitForTimeout(2000);
		await page.screenshot({ path: 'screenshots/02-investigations.png', fullPage: true });

		// Analytics
		await page.goto(`${FRONTEND_URL}/analytics`);
		await page.waitForLoadState('domcontentloaded');
		await page.waitForTimeout(2000);
		await page.screenshot({ path: 'screenshots/03-analytics.png', fullPage: true });

		// Audit
		await page.goto(`${FRONTEND_URL}/audit`);
		await page.waitForLoadState('domcontentloaded');
		await page.waitForTimeout(2000);
		await page.screenshot({ path: 'screenshots/04-audit.png', fullPage: true });

		// Review
		await page.goto(`${FRONTEND_URL}/review`);
		await page.waitForLoadState('domcontentloaded');
		await page.waitForTimeout(2000);
		await page.screenshot({ path: 'screenshots/05-review.png', fullPage: true });
	});
});
