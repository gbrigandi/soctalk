import { test } from '@playwright/test';

// Throwaway screenshot helper for the live IAM demo; gated off the default suite (not committed).
test.skip(!process.env.IAM_LIVE, 'live-only screenshot helper');

test('iam screenshot', async ({ page }) => {
	const email = `demo-${Date.now()}@iam.example`;
	await page.goto('/login');
	await page.locator('input[type=email]').fill('admin@iam.example');
	await page.locator('input[type=password]').fill('Admin-pw-123456');
	await page.getByRole('button', { name: 'Sign in' }).click();
	await page.getByRole('link', { name: 'Staff Users' }).click();
	await page.getByRole('button', { name: '+ Add user' }).click();
	await page.getByPlaceholder('analyst@your-mssp.example').fill(email);
	await page.locator('select').last().selectOption('mssp_manager');
	await page.getByRole('button', { name: 'Create user' }).click();
	await page.getByText('One-time temporary password').waitFor();
	await page.screenshot({ path: 'test-results/iam-staff-users.png', fullPage: true });
});
