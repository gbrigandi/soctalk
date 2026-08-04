// Run the frontend E2E from THIS machine against the app served on the NUC
// (over the tailnet). The runner lives outside the NUC; the app-under-test runs
// on the NUC. API is mocked in-browser via page.route, so no backend is needed.
//   NUC_BASE_URL=http://100.102.223.8:4173 pnpm exec playwright test --config playwright.nuc.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	testDir: './tests',
	testMatch: /run-budget\.spec\.ts/,
	fullyParallel: false,
	reporter: 'list',
	use: {
		baseURL: process.env.NUC_BASE_URL || 'http://100.102.223.8:4173',
		trace: 'off',
		screenshot: 'only-on-failure'
	},
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }]
});
