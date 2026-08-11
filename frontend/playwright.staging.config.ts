// Run the frontend E2E from THIS machine against the app served on the staging
// host (over the operator's overlay network). The runner lives outside staging;
// the app-under-test runs on staging. API is mocked in-browser via page.route,
// so no backend is needed.
//   STAGING_BASE_URL=http://<staging-host>:4173 pnpm exec playwright test --config playwright.staging.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	testDir: './tests',
	testMatch: /run-budget\.spec\.ts/,
	fullyParallel: false,
	reporter: 'list',
	use: {
		baseURL: process.env.STAGING_BASE_URL,
		trace: 'off',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
