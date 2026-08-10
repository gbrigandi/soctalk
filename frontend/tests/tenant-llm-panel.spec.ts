import { test, expect, type Page } from '@playwright/test';

/**
 * Tenant detail — "LLM Configuration" panel (feature tenant.llm.detail-panel).
 *
 * Covers:
 *  - the masked read view renders provider/base_url/model + the key preview
 *    (has_api_key=true) or the shared-install-key messaging (false),
 *  - the inline Edit form PATCHes .../llm with ONLY the changed fields and a
 *    blank "Replace API key" field is OMITTED from the payload,
 *  - the clear-key flow requires an inline confirm before DELETE .../llm/api-key
 *    and the panel re-fetches into the shared-key state afterwards,
 *  - the plaintext key NEVER appears in the page content in any state.
 *
 * The whole /api surface is mocked at the browser so neither the FastAPI
 * backend nor Postgres need to be up (mirrors tenant-external-siem-panel.spec.ts).
 */

const TENANT_ID = '33333333-3333-3333-3333-333333333333';

// A "plaintext" key the operator types — must never surface in the DOM.
const SECRET_KEY = 'sk-PLAINTEXT-SECRET-do-not-render-1234567890';

const MSSP_USER = {
	user_id: '00000000-0000-0000-0000-000000000001',
	email: 'admin@mssp.example',
	user_type: 'mssp_admin',
	role: 'mssp_admin',
	tenant_id: null,
	current_tenant: null
};

// poc profile on purpose — the panel must be visible for ANY profile.
const TENANT = {
	id: TENANT_ID,
	slug: 'acme',
	display_name: 'Acme Corp',
	state: 'active',
	profile: 'poc',
	created_at: '2026-01-01T00:00:00Z',
	state_changed_at: '2026-01-01T00:00:00Z',
	runtime: null
};

// Mirrors the backend LlmConfigRead shape, including the per-tier model
// overrides (null = no override, the tier falls back to ``model``).
interface LlmRead {
	provider: string;
	base_url: string;
	model: string;
	engine: string | null;
	fast_model: string | null;
	reasoning_model: string | null;
	temperature: number;
	max_tokens: number;
	dollar_budget_per_run: number | null;
	token_budget_per_run: number | null;
	has_api_key: boolean;
	api_key_preview: string;
}

const LLM_READ_WITH_KEY: LlmRead = {
	provider: 'openai-compatible',
	base_url: 'https://llm.acme.example/v1',
	model: 'gpt-4o-mini',
	engine: null,
	fast_model: null,
	reasoning_model: null,
	temperature: 0.0,
	max_tokens: 4096,
	dollar_budget_per_run: null,
	token_budget_per_run: null,
	has_api_key: true,
	api_key_preview: 'sk-…7890'
};

const LLM_READ_NO_KEY: LlmRead = {
	provider: 'openai-compatible',
	base_url: 'https://llm.acme.example/v1',
	model: 'gpt-4o-mini',
	engine: null,
	fast_model: null,
	reasoning_model: null,
	temperature: 0.0,
	max_tokens: 4096,
	dollar_budget_per_run: null,
	token_budget_per_run: null,
	has_api_key: false,
	api_key_preview: ''
};

// Both per-tier overrides set — exercises the override read view and the
// clear-to-default ('' sent) edit path.
const LLM_READ_WITH_OVERRIDES: LlmRead = {
	...LLM_READ_WITH_KEY,
	fast_model: 'gpt-4o-mini-fast',
	reasoning_model: 'o3-deep-thought'
};

const LLM_READ_WITH_ENGINE: LlmRead = {
	...LLM_READ_WITH_KEY,
	engine: 'sglang'
};

const SIEM_READ = {
	indexer_url: 'https://indexer.acme:9200',
	indexer_username: 'indexer-ro',
	api_url: 'https://wazuh.acme:55000',
	api_username: 'soctalk-adapter',
	has_indexer_password: true,
	has_api_password: true,
	has_api_token: false,
	verify_ssl: true
};

interface MockHandles {
	lastPatchBody: () => Record<string, unknown> | null;
	patchCount: () => number;
	clearCount: () => number;
}

/** Mock every /api call; returns handles to inspect PATCH / DELETE traffic. */
async function mockApi(page: Page, initialRead: LlmRead = LLM_READ_WITH_KEY): Promise<MockHandles> {
	let lastPatchBody: Record<string, unknown> | null = null;
	let patchCount = 0;
	let clearCount = 0;
	// Mutable so a PATCH / DELETE flips the masked state on the subsequent read.
	let llmRead = { ...initialRead };

	await page.route('**/api/**', async (route) => {
		const req = route.request();
		const method = req.method();
		const path = new URL(req.url()).pathname;
		// The '**/api/**' glob also matches Vite dev-module URLs such as
		// /src/lib/api/client.ts — let anything that is not a real backend
		// call fall through so the app bundle can load.
		if (!path.startsWith('/api/')) return route.continue();
		const json = (body: unknown, status = 200) =>
			route.fulfill({
				status,
				contentType: 'application/json',
				body: JSON.stringify(body)
			});

		if (path.includes('/auth/me')) return json(MSSP_USER);
		if (path.includes('/events/stream')) {
			return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
		}
		if (path.endsWith('/adapter-status')) return json({ reachable: true, ok: true });
		if (path.endsWith('/external-siem')) return json(SIEM_READ);
		if (path.endsWith('/llm/api-key') && method === 'DELETE') {
			clearCount += 1;
			llmRead = { ...llmRead, has_api_key: false, api_key_preview: '' };
			return route.fulfill({ status: 204, body: '' });
		}
		if (path.endsWith('/llm')) {
			if (method === 'PATCH') {
				patchCount += 1;
				lastPatchBody = req.postDataJSON();
				const body = lastPatchBody as Record<string, unknown>;
				llmRead = {
					...llmRead,
					...(body.provider !== undefined ? { provider: body.provider as string } : {}),
					...(body.base_url !== undefined ? { base_url: body.base_url as string } : {}),
					...(body.model !== undefined ? { model: body.model as string } : {}),
					...(body.engine !== undefined ? { engine: (body.engine as string) || null } : {}),
					// ''-clears semantics: an empty string NULLs the override server-side.
					...(body.fast_model !== undefined
						? { fast_model: (body.fast_model as string) || null }
						: {}),
					...(body.reasoning_model !== undefined
						? { reasoning_model: (body.reasoning_model as string) || null }
						: {}),
					...(body.temperature !== undefined
						? { temperature: body.temperature as number }
						: {}),
					...(body.max_tokens !== undefined
						? { max_tokens: body.max_tokens as number }
						: {}),
					// Budget caps are tri-state; null clears back to the default.
					...('dollar_budget_per_run' in body
						? { dollar_budget_per_run: body.dollar_budget_per_run as number | null }
						: {}),
					...('token_budget_per_run' in body
						? { token_budget_per_run: body.token_budget_per_run as number | null }
						: {}),
					// A sent key is masked server-side: presence flag + tail preview only.
					...(body.api_key !== undefined
						? {
								has_api_key: true,
								api_key_preview: `sk-…${String(body.api_key).slice(-4)}`
							}
						: {})
				};
				return json(llmRead);
			}
			return json(llmRead);
		}
		// /tenants/<id>/events
		if (/\/tenants\/[^/]+\/events$/.test(path)) return json([]);
		// /tenants/<id> (detail)
		if (/\/tenants\/[^/]+$/.test(path) && method === 'GET') return json(TENANT);
		// /tenants (list)
		if (path.endsWith('/tenants') && method === 'GET') return json([TENANT]);
		return json({});
	});

	return {
		lastPatchBody: () => lastPatchBody,
		patchCount: () => patchCount,
		clearCount: () => clearCount
	};
}

test.describe('Tenant detail — LLM Configuration panel', () => {
	test('renders the masked read view with the key preview when a key is set', async ({ page }) => {
		await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);

		const panel = page.getByTestId('llm-config-panel');
		await expect(panel).toBeVisible();
		await expect(panel.getByText('LLM Configuration')).toBeVisible();

		await expect(page.getByTestId('llm-provider')).toContainText('openai-compatible');
		await expect(page.getByTestId('llm-base-url')).toContainText('https://llm.acme.example/v1');
		await expect(page.getByTestId('llm-model')).toContainText('gpt-4o-mini');
		await expect(page.getByTestId('llm-engine')).toContainText('auto');
		// No per-tier overrides → the effective primary model is shown as the
		// "default (<model>)" fallback so the operator sees what will be used.
		await expect(page.getByTestId('llm-fast-model')).toContainText('default (gpt-4o-mini)');
		await expect(page.getByTestId('llm-reasoning-model')).toContainText('default (gpt-4o-mini)');
		// has_api_key=true → masked preview, NOT the shared-key messaging.
		await expect(page.getByTestId('llm-api-key-preview')).toContainText('sk-…7890');
		await expect(page.getByTestId('llm-shared-key-note')).toHaveCount(0);
		// Clear-key affordance exists only when a key is present.
		await expect(page.getByTestId('llm-clear-key')).toBeVisible();
		// Rollout semantics note — covers the per-tier override rollout too.
		await expect(page.getByTestId('llm-rollout-note')).toContainText('re-render of the tenant release');
		await expect(page.getByTestId('llm-rollout-note')).toContainText('fast/thinking model');
		await expect(page.getByTestId('llm-rollout-note')).toContainText('within seconds');
	});

	test('shows the shared-install-key messaging when no tenant key is set', async ({ page }) => {
		await mockApi(page, LLM_READ_NO_KEY);
		await page.goto(`/tenants/${TENANT_ID}`);

		await expect(page.getByTestId('llm-config-panel')).toBeVisible();
		await expect(page.getByTestId('llm-shared-key-note')).toContainText(
			'using MSSP shared install key'
		);
		await expect(page.getByTestId('llm-api-key-preview')).toHaveCount(0);
		await expect(page.getByTestId('llm-clear-key')).toHaveCount(0);
	});

	test('renders set fast/thinking model overrides instead of the default fallback', async ({
		page
	}) => {
		await mockApi(page, LLM_READ_WITH_OVERRIDES);
		await page.goto(`/tenants/${TENANT_ID}`);

		await expect(page.getByTestId('llm-config-panel')).toBeVisible();
		await expect(page.getByTestId('llm-fast-model')).toContainText('gpt-4o-mini-fast');
		await expect(page.getByTestId('llm-reasoning-model')).toContainText('o3-deep-thought');
		await expect(page.getByTestId('llm-fast-model')).not.toContainText('default (');
		await expect(page.getByTestId('llm-reasoning-model')).not.toContainText('default (');
	});

	test('setting a fast model PATCHes exactly { fast_model } and re-renders the read', async ({
		page
	}) => {
		const handles = await mockApi(page); // overrides start null
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		await expect(page.getByTestId('llm-edit-form')).toBeVisible();
		// Inputs are seeded from the read — null overrides seed as empty.
		await expect(page.locator('input[name="fast_model"]')).toHaveValue('');
		await expect(page.locator('input[name="reasoning_model"]')).toHaveValue('');

		// Set ONLY the fast model (with whitespace — the panel trims it).
		await page.fill('input[name="fast_model"]', '  gpt-4o-mini-fast  ');

		const patchReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname.endsWith('/llm') && r.method() === 'PATCH'
		);
		await page.getByTestId('llm-save').click();
		await patchReq;

		await expect.poll(() => handles.lastPatchBody()).not.toBeNull();
		// Exactly { fast_model } — reasoning_model stayed empty over a null read
		// so it is OMITTED (unchanged), never sent as a spurious clear.
		expect(handles.lastPatchBody()).toEqual({ fast_model: 'gpt-4o-mini-fast' });

		// The read view re-renders from the PATCH response.
		await expect(page.getByTestId('llm-fast-model')).toContainText('gpt-4o-mini-fast');
		await expect(page.getByTestId('llm-reasoning-model')).toContainText('default (gpt-4o-mini)');
	});

	test('renders global sampling and PATCHes exactly { temperature, max_tokens } when changed', async ({
		page
	}) => {
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();
		// Read view shows the defaults.
		await expect(page.getByTestId('llm-temperature')).toContainText('0');
		await expect(page.getByTestId('llm-max-tokens')).toContainText('4096');

		await page.getByTestId('llm-edit').click();
		await expect(page.locator('input[name="temperature"]')).toHaveValue('0');
		await expect(page.locator('input[name="max_tokens"]')).toHaveValue('4096');

		await page.fill('input[name="temperature"]', '0.7');
		await page.fill('input[name="max_tokens"]', '512');

		const patchReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname.endsWith('/llm') && r.method() === 'PATCH'
		);
		await page.getByTestId('llm-save').click();
		await patchReq;

		await expect.poll(() => handles.lastPatchBody()).not.toBeNull();
		// Only the two changed sampling fields ride along.
		expect(handles.lastPatchBody()).toEqual({ temperature: 0.7, max_tokens: 512 });
		await expect(page.getByTestId('llm-temperature')).toContainText('0.7');
		await expect(page.getByTestId('llm-max-tokens')).toContainText('512');
	});

	test('out-of-range temperature blocks the PATCH', async ({ page }) => {
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		await page.fill('input[name="temperature"]', '3');
		await page.getByTestId('llm-save').click();

		await expect(page.getByTestId('llm-form-error')).toContainText('between 0 and 2');
		expect(handles.patchCount()).toBe(0);
	});

	test('clearing the base URL blocks the PATCH with a designed error', async ({ page }) => {
		// The API's tri-state PATCH rejects "" and treats null as unchanged, so
		// a cleared field cannot express "no base URL" — without the client-side
		// gate the raw pydantic 422 reached the operator (#142 matrix finding).
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		await page.fill('input[name="base_url"]', '');
		await page.getByTestId('llm-save').click();

		await expect(page.getByTestId('llm-form-error')).toContainText('cannot be cleared');
		expect(handles.patchCount()).toBe(0);
	});

	test('the deprecated dollar-budget control is gone from this panel', async ({ page }) => {
		// It moved to the run-budget resource (#128). It used to render to worker
		// env, so changing it needed a rollout and the value never reached the run
		// row -- the row showed $5 while the worker enforced something else. The
		// API now 422s on the field, so leaving the control here would make an
		// operator's save fail.
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();
		await expect(page.getByTestId('llm-dollar-budget')).toHaveCount(0);

		await page.getByTestId('llm-edit').click();
		await expect(page.locator('input[name="dollar_budget"]')).toHaveCount(0);
		expect(handles.patchCount()).toBe(0);
	});

	test('blanking a sampling field is rejected, not a silent no-op', async ({ page }) => {
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		// Clear temperature — a blank read as "reset" would silently do nothing;
		// the panel must surface it as required instead.
		await page.fill('input[name="temperature"]', '');
		await page.getByTestId('llm-save').click();

		await expect(page.getByTestId('llm-form-error')).toContainText('Temperature is required');
		expect(handles.patchCount()).toBe(0);
		await expect(page.getByTestId('llm-edit-form')).toBeVisible();
	});

	test('clearing the thinking model sends reasoning_model: "" and no other model fields', async ({
		page
	}) => {
		const handles = await mockApi(page, LLM_READ_WITH_OVERRIDES);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		await expect(page.getByTestId('llm-edit-form')).toBeVisible();
		// Seeded from the read — set overrides appear in the inputs.
		await expect(page.locator('input[name="fast_model"]')).toHaveValue('gpt-4o-mini-fast');
		await expect(page.locator('input[name="reasoning_model"]')).toHaveValue('o3-deep-thought');

		// Empty the thinking model input — clears the override to default.
		await page.fill('input[name="reasoning_model"]', '');

		const patchReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname.endsWith('/llm') && r.method() === 'PATCH'
		);
		await page.getByTestId('llm-save').click();
		await patchReq;

		await expect.poll(() => handles.lastPatchBody()).not.toBeNull();
		const body = handles.lastPatchBody() as Record<string, unknown>;
		// '' clears the override server-side; nothing else changed so no other
		// model fields ride along.
		expect(body.reasoning_model).toBe('');
		expect(body).not.toHaveProperty('fast_model');
		expect(body).not.toHaveProperty('model');
		expect(body).not.toHaveProperty('provider');
		expect(body).not.toHaveProperty('base_url');
		expect(body).not.toHaveProperty('api_key');

		// Read view shows the cleared tier falling back to the primary model.
		await expect(page.getByTestId('llm-reasoning-model')).toContainText('default (gpt-4o-mini)');
		await expect(page.getByTestId('llm-fast-model')).toContainText('gpt-4o-mini-fast');
	});

	test('PATCH carries ONLY the changed fields; a blank key is omitted', async ({ page }) => {
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		await expect(page.getByTestId('llm-edit-form')).toBeVisible();

		// Provider select + password-type key input are present.
		await expect(page.locator('select[name="provider"]')).toBeVisible();
		await expect(page.locator('input[name="api_key"]')).toHaveAttribute('type', 'password');

		// Change ONLY the model; leave provider/base_url/key untouched.
		await page.fill('input[name="model"]', 'claude-sonnet-4');

		const patchReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname.endsWith('/llm') && r.method() === 'PATCH'
		);
		await page.getByTestId('llm-save').click();
		await patchReq;

		await expect.poll(() => handles.lastPatchBody()).not.toBeNull();
		// Exactly { model } — no api_key, no provider, no base_url.
		expect(handles.lastPatchBody()).toEqual({ model: 'claude-sonnet-4' });

		// Read view returns with the refreshed masked state.
		await expect(page.getByTestId('llm-model')).toContainText('claude-sonnet-4');
		await expect(page.locator('input[name="api_key"]')).toHaveCount(0);
	});

	test('clearing the primary engine sends engine: "" and re-renders auto', async ({ page }) => {
		const handles = await mockApi(page, LLM_READ_WITH_ENGINE);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();
		await expect(page.getByTestId('llm-engine')).toContainText('sglang');

		await page.getByTestId('llm-edit').click();
		await expect(page.locator('select[name="engine"]')).toHaveValue('sglang');
		await page.selectOption('select[name="engine"]', '');

		const patchReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname.endsWith('/llm') && r.method() === 'PATCH'
		);
		await page.getByTestId('llm-save').click();
		await patchReq;

		await expect.poll(() => handles.lastPatchBody()).not.toBeNull();
		expect(handles.lastPatchBody()).toEqual({ engine: '' });
		await expect(page.getByTestId('llm-engine')).toContainText('auto');
	});

	test('replacing the key sends api_key but the plaintext NEVER appears in the page', async ({
		page
	}) => {
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		await page.fill('input[name="api_key"]', SECRET_KEY);

		// Even while typed, the password input must not leak the key into the DOM.
		expect(await page.content()).not.toContain(SECRET_KEY);

		const patchReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname.endsWith('/llm') && r.method() === 'PATCH'
		);
		await page.getByTestId('llm-save').click();
		await patchReq;

		await expect.poll(() => handles.lastPatchBody()).not.toBeNull();
		expect(handles.lastPatchBody()).toEqual({ api_key: SECRET_KEY });

		// Back to the read view — only the masked tail is rendered.
		await expect(page.getByTestId('llm-api-key-preview')).toContainText('sk-…7890');
		expect(await page.content()).not.toContain(SECRET_KEY);
	});

	test('base URL must start with http(s):// — invalid value blocks the PATCH', async ({
		page
	}) => {
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();

		await page.getByTestId('llm-edit').click();
		await page.fill('input[name="base_url"]', 'ftp://bad.example');
		await page.getByTestId('llm-save').click();

		await expect(page.getByTestId('llm-form-error')).toContainText('http:// or https://');
		expect(handles.patchCount()).toBe(0);
		// The form stays open for correction.
		await expect(page.getByTestId('llm-edit-form')).toBeVisible();
	});

	test('clear-key requires confirm, DELETEs, then shows the shared-key state', async ({
		page
	}) => {
		const handles = await mockApi(page);
		await page.goto(`/tenants/${TENANT_ID}`);
		await expect(page.getByTestId('llm-config-panel')).toBeVisible();
		await expect(page.getByTestId('llm-api-key-preview')).toContainText('sk-…7890');

		// First click only reveals the confirm step — no DELETE yet.
		await page.getByTestId('llm-clear-key').click();
		await expect(page.getByTestId('llm-clear-key-confirm-row')).toBeVisible();
		expect(handles.clearCount()).toBe(0);

		// Cancel backs out without a DELETE.
		await page.getByTestId('llm-clear-key-cancel').click();
		await expect(page.getByTestId('llm-clear-key-confirm-row')).toHaveCount(0);
		expect(handles.clearCount()).toBe(0);

		// Confirm path fires the DELETE and the panel re-fetches into shared-key.
		await page.getByTestId('llm-clear-key').click();
		const delReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname.endsWith('/llm/api-key') && r.method() === 'DELETE'
		);
		await page.getByTestId('llm-clear-key-confirm').click();
		await delReq;

		await expect.poll(() => handles.clearCount()).toBe(1);
		await expect(page.getByTestId('llm-shared-key-note')).toContainText(
			'using MSSP shared install key'
		);
		await expect(page.getByTestId('llm-api-key-preview')).toHaveCount(0);
		await expect(page.getByTestId('llm-clear-key')).toHaveCount(0);
	});
});
