// i18n end-to-end (#52): every internationalized screen across ALL locales.
//
// Hermetic like the other specs — auth + data come from page.route mocks, so
// the suite runs on the vite dev server alone. Structural assertions go
// through data-testids; the point of THIS suite is the rendered text, so the
// expected strings are asserted verbatim from the catalogs. If a catalog
// string changes, the matching row here changes with it — that is the
// contract under test.
import { test, expect, type Page } from '@playwright/test';

const TID = '11111111-1111-1111-1111-111111111111';

// Expected strings per locale, verbatim from frontend/messages/{locale}.json.
const LOCALES = [
	{
		prefix: '',
		tag: 'en-US (unprefixed default)',
		login: 'Sign in',
		submit: 'Sign in',
		email: 'Email',
		navInvestigations: 'Investigations',
		navRp: 'Response Playbooks',
		navSettings: 'Settings',
		rpNew: '+ New response playbook',
		rpEmpty: 'No response playbooks yet.',
		editorTitle: 'New response playbook',
		identity: 'Identity',
		appliesTo: 'Applies to',
		capAnnotate: 'Annotate investigation',
		capExternal: 'External action (gated)',
		gatedHint: 'Gated: this action routes to a human-approved proposal before it executes.',
		slugError: 'id must be a slug: lowercase letters, digits, hyphens.',
		flow: 'Flow',
		flowDisposition: 'Effective disposition',
		firesAuto: 'fires automatically'
	},
	{
		prefix: '/pt-br',
		tag: 'pt-BR',
		login: 'Entrar',
		submit: 'Entrar',
		email: 'E-mail',
		navInvestigations: 'Investigações',
		navRp: 'Playbooks de Resposta',
		navSettings: 'Configurações',
		rpNew: '+ Novo playbook de resposta',
		rpEmpty: 'Ainda não há playbooks de resposta.',
		editorTitle: 'Novo playbook de resposta',
		identity: 'Identidade',
		appliesTo: 'Aplica-se a',
		capAnnotate: 'Anotar investigação',
		capExternal: 'Ação externa (com aprovação)',
		gatedHint:
			'Controlada: esta ação passa por uma proposta aprovada por um humano antes de executar.',
		slugError: 'o id deve ser um slug: letras minúsculas, dígitos e hifens.',
		flow: 'Fluxo',
		flowDisposition: 'Disposição efetiva',
		firesAuto: 'dispara automaticamente'
	},
	{
		prefix: '/es-419',
		tag: 'es-419',
		login: 'Iniciar sesión',
		submit: 'Iniciar sesión',
		email: 'Correo electrónico',
		navInvestigations: 'Investigaciones',
		navRp: 'Playbooks de respuesta',
		navSettings: 'Configuración',
		rpNew: '+ Nuevo playbook de respuesta',
		rpEmpty: 'Aún no hay playbooks de respuesta.',
		editorTitle: 'Nuevo playbook de respuesta',
		identity: 'Identidad',
		appliesTo: 'Se aplica a',
		capAnnotate: 'Anotar investigación',
		capExternal: 'Acción externa (con aprobación)',
		gatedHint:
			'Controlada: esta acción pasa por una propuesta aprobada por un humano antes de ejecutarse.',
		slugError: 'el id debe ser un slug: minúsculas, dígitos y guiones.',
		flow: 'Flujo',
		flowDisposition: 'Disposición efectiva',
		firesAuto: 'se dispara automáticamente'
	},
	{
		prefix: '/zh-cn',
		tag: 'zh-CN',
		login: '登录',
		submit: '登录',
		email: '邮箱',
		navInvestigations: '调查',
		navRp: '响应剧本',
		navSettings: '设置',
		rpNew: '+ 新建响应剧本',
		rpEmpty: '尚无响应剧本。',
		editorTitle: '新建响应剧本',
		identity: '标识',
		appliesTo: '适用范围',
		capAnnotate: '备注调查',
		capExternal: '外部操作（需审批）',
		gatedHint: '受控操作：执行前需经人工批准的提案。',
		slugError: 'id 必须是 slug：小写字母、数字和连字符。',
		flow: '流程',
		flowDisposition: '最终处置',
		firesAuto: '自动触发'
	},
	{
		prefix: '/fr-fr',
		tag: 'fr-FR',
		login: 'Connexion',
		submit: 'Se connecter',
		email: 'E-mail',
		navInvestigations: 'Investigations',
		navRp: 'Playbooks de réponse',
		navSettings: 'Paramètres',
		rpNew: '+ Nouveau playbook de réponse',
		rpEmpty: `Aucun playbook de réponse pour l'instant.`,
		editorTitle: 'Nouveau playbook de réponse',
		identity: 'Identité',
		appliesTo: `S'applique à`,
		capAnnotate: `Annoter l'investigation`,
		capExternal: 'Action externe (contrôlée)',
		gatedHint: `Contrôlée : cette action passe par une proposition approuvée par un humain avant de s'exécuter.`,
		slugError: `l'id doit être un slug : minuscules, chiffres, tirets.`,
		flow: 'Flux',
		flowDisposition: 'Disposition effective',
		firesAuto: 'se déclenche automatiquement'
	},
	{
		prefix: '/de-de',
		tag: 'de-DE',
		login: 'Anmelden',
		submit: 'Anmelden',
		email: 'E-Mail',
		navInvestigations: 'Untersuchungen',
		navRp: 'Response-Playbooks',
		navSettings: 'Einstellungen',
		rpNew: '+ Neues Response-Playbook',
		rpEmpty: 'Noch keine Response-Playbooks.',
		editorTitle: 'Neues Response-Playbook',
		identity: 'Identität',
		appliesTo: 'Gilt für',
		capAnnotate: 'Untersuchung annotieren',
		capExternal: 'Externe Aktion (kontrolliert)',
		gatedHint:
			'Kontrolliert: Diese Aktion durchläuft vor der Ausführung einen von Menschen freizugebenden Vorschlag.',
		slugError: 'Die ID muss ein Slug sein: Kleinbuchstaben, Ziffern, Bindestriche.',
		flow: 'Ablauf',
		flowDisposition: 'Effektive Disposition',
		firesAuto: 'löst automatisch aus'
	},
	{
		prefix: '/it-it',
		tag: 'it-IT',
		login: 'Accedi',
		submit: 'Accedi',
		email: 'E-mail',
		navInvestigations: 'Indagini',
		navRp: 'Playbook di risposta',
		navSettings: 'Impostazioni',
		rpNew: '+ Nuovo playbook di risposta',
		rpEmpty: 'Ancora nessun playbook di risposta.',
		editorTitle: 'Nuovo playbook di risposta',
		identity: 'Identità',
		appliesTo: 'Si applica a',
		capAnnotate: 'Annota indagine',
		capExternal: 'Azione esterna (controllata)',
		gatedHint: `Controllata: questa azione passa per una proposta approvata da un umano prima di essere eseguita.`,
		slugError: `l'id deve essere uno slug: minuscole, cifre e trattini.`,
		flow: 'Flusso',
		flowDisposition: 'Disposizione effettiva',
		firesAuto: 'scatta automaticamente'
	},
	{
		prefix: '/zh-tw',
		tag: 'zh-TW',
		login: '登入',
		submit: '登入',
		email: '電子郵件',
		navInvestigations: '調查',
		navRp: '應變劇本',
		navSettings: '設定',
		rpNew: '+ 新增應變劇本',
		rpEmpty: '尚無應變劇本。',
		editorTitle: '新增應變劇本',
		identity: '識別資訊',
		appliesTo: '適用於',
		capAnnotate: '為調查加註',
		capExternal: '外部動作 (需審核)',
		gatedHint: '需審核: 此動作會在執行前轉為需人工核准的提案。',
		slugError: 'id 必須是 slug: 小寫字母、數字、連字號。',
		flow: '流程',
		flowDisposition: '實際處置',
		firesAuto: '自動觸發'
	}
] as const;

async function mockAuthed(page: Page) {
	await page.route('**/auth/me', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				user_id: 'u1',
				email: 'admin@mssp.example',
				user_type: 'mssp',
				role: 'mssp_admin',
				tenant_id: null,
				current_tenant: TID,
				current_tenant_slug: 'acme',
				permissions: [
					'view_investigations',
					'triage_investigation',
					'review_decide',
					'view_alerts',
					'view_dashboard',
					'view_analytics',
					'view_audit',
					'use_chat',
					'view_triage_policies',
					'view_tenants',
					'manage_triage_policies'
				]
			})
		})
	);
	await page.route('**/mssp/tenants/*/response-playbooks', (route) =>
		route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
	);
}

async function mockAnonymous(page: Page) {
	await page.route('**/auth/me', (route) =>
		route.fulfill({
			status: 401,
			contentType: 'application/json',
			body: JSON.stringify({ detail: 'authentication required' })
		})
	);
}

for (const L of LOCALES) {
	test.describe(`i18n ${L.tag}`, () => {
		test(`login page is localized`, async ({ page }) => {
			await mockAnonymous(page);
			await page.goto(`${L.prefix}/login`);
			await expect(page.locator('h1')).toHaveText(L.login);
			await expect(page.locator('form .label span').first()).toHaveText(L.email);
			await expect(page.locator('button[type="submit"]')).toContainText(L.submit);
		});

		test(`nav + response-playbooks list are localized, links keep the prefix`, async ({
			page
		}) => {
			await mockAuthed(page);
			await page.goto(`${L.prefix}/response-playbooks`);
			// nav labels
			await expect(page.locator('aside a', { hasText: L.navInvestigations }).first()).toBeVisible();
			await expect(page.locator('aside a', { hasText: L.navSettings }).first()).toBeVisible();
			// list surface
			await expect(page.locator('h1')).toHaveText(L.navRp);
			await expect(page.getByRole('link', { name: L.rpNew })).toBeVisible();
			await expect(page.getByText(L.rpEmpty)).toBeVisible();
			// nav hrefs carry the locale (en stays bare; /en-us canonicalizes too)
			const investigationsHref = await page
				.locator('aside a', { hasText: L.navInvestigations })
				.first()
				.getAttribute('href');
			expect(investigationsHref).toBe(`${L.prefix}/investigations`);
		});

		test(`no-code editor is localized end-to-end`, async ({ page }) => {
			test.slow(); // the flow canvas re-layouts on every edit
			await mockAuthed(page);
			await page.setViewportSize({ width: 1600, height: 1000 });
			await page.goto(`${L.prefix}/response-playbooks/editor`);
			await page.waitForSelector('[data-testid="response-editor"]');

			await expect(page.locator('h1')).toHaveText(L.editorTitle);
			await expect(page.getByRole('heading', { name: L.identity })).toBeVisible();
			await expect(page.getByRole('heading', { name: L.appliesTo })).toBeVisible();

			// capability labels resolve through capLabel() at render time
			const capSelect = page.locator('[data-testid="rp-escalate-cap-0"]');
			await expect(capSelect.locator('option').first()).toHaveText(L.capAnnotate);
			await expect(capSelect.locator('option').nth(2)).toContainText(L.capExternal);

			// gated hint appears when the gated capability is selected
			await capSelect.selectOption('external_action');
			await expect(page.getByText(L.gatedHint)).toBeVisible();

			// fail-closed validation renders localized
			await page.locator('[data-testid="rp-id"]').fill('');
			await expect(page.getByText(L.slugError)).toBeVisible();
			await page.locator('[data-testid="rp-id"]').fill('valid-slug');

			// flow preview: panel title, envelope node, autonomous subtitle
			const flowAside = page.locator('aside').filter({ hasText: L.flow });
			await expect(flowAside.getByText(L.flowDisposition)).toBeVisible();
			await capSelect.selectOption('annotate_investigation');
			await expect(flowAside.getByText(L.firesAuto).first()).toBeVisible();
		});
	});
}

test.describe('i18n routing behavior', () => {
	test('locale switcher relocalizes in place and sets the cookie', async ({ page }) => {
		await mockAuthed(page);
		await page.goto('/response-playbooks');
		await expect(page.locator('h1')).toHaveText('Response Playbooks');
		await page.locator('[data-testid="locale-switcher"]').selectOption('pt-BR');
		await page.waitForURL((u) => u.pathname === '/pt-br/response-playbooks');
		await expect(page.locator('h1')).toHaveText('Playbooks de Resposta');
		const cookies = await page.context().cookies();
		expect(cookies.find((c) => c.name === 'PARAGLIDE_LOCALE')?.value).toBe('pt-BR');
	});

	test('cookie redirects an unprefixed entry to the chosen locale', async ({ page }) => {
		await mockAuthed(page);
		await page.context().addCookies([
			{ name: 'PARAGLIDE_LOCALE', value: 'zh-CN', domain: 'localhost', path: '/' }
		]);
		await page.goto('/response-playbooks');
		await page.waitForURL((u) => u.pathname === '/zh-cn/response-playbooks');
		await expect(page.locator('h1')).toHaveText('响应剧本');
	});

	test('explicit URL locale beats the cookie', async ({ page }) => {
		await mockAuthed(page);
		await page.context().addCookies([
			{ name: 'PARAGLIDE_LOCALE', value: 'zh-CN', domain: 'localhost', path: '/' }
		]);
		await page.goto('/pt-br/response-playbooks');
		await expect(page).toHaveURL(/\/pt-br\/response-playbooks/);
		await expect(page.locator('h1')).toHaveText('Playbooks de Resposta');
	});

	test('/en-us is accepted and canonicalizes links to unprefixed', async ({ page }) => {
		await mockAuthed(page);
		await page.goto('/en-us/response-playbooks');
		await expect(page.locator('h1')).toHaveText('Response Playbooks');
		const href = await page
			.locator('aside a', { hasText: 'Investigations' })
			.first()
			.getAttribute('href');
		expect(href).toBe('/investigations');
	});

	test('in-app navigation keeps the locale prefix', async ({ page }) => {
		await mockAuthed(page);
		await page.goto('/pt-br/response-playbooks');
		const settings = page.locator('aside a', { hasText: 'Configurações' }).first();
		await expect(settings).toBeVisible();
		await settings.click();
		await expect(page).toHaveURL(/\/pt-br\/settings/, { timeout: 15000 });
	});
});
