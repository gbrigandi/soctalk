import { test } from '@playwright/test';

const TID = '11111111-1111-1111-1111-111111111111';
const PERMS = [
	'view_authorization_facts', 'manage_authorization_facts', 'view_tenants', 'view_dashboard',
	'view_engagements', 'authorize_engagement'
];

test('screenshot the MSSP engagements tab', async ({ page }) => {
	await page.route('**/auth/me', (r) =>
		r.fulfill({
			status: 200, contentType: 'application/json', body: JSON.stringify({
				user_id: 'u1', email: 'admin@mssp.example', user_type: 'mssp', role: 'mssp_admin',
				tenant_id: null, current_tenant: TID, current_tenant_slug: 'acme', permissions: PERMS
			})
		})
	);
	await page.route('**/api/mssp/tenants/*/engagements*', (r) =>
		r.fulfill({
			status: 200, contentType: 'application/json', body: JSON.stringify([
				{
					id: 'eng-1', name: 'Q3 external pentest', kind: 'pentest',
					starts_at: '2026-08-01T09:00:00Z', ends_at: '2026-08-05T18:00:00Z',
					scope_source_ips: ['203.0.113.0/24'], scope_hosts: ['web-01'], scope_techniques: ['T1078'],
					revoked_at: null, created_at: '2026-07-20T00:00:00Z',
					declared_test_count: 3, out_of_scope_count: 2
				},
				{
					id: 'eng-2', name: 'Payments red team', kind: 'red_team',
					starts_at: '2026-07-01T09:00:00Z', ends_at: '2026-07-09T18:00:00Z',
					scope_source_ips: ['198.51.100.7'], scope_hosts: [], scope_techniques: ['T1110.001'],
					revoked_at: '2026-07-05T00:00:00Z', created_at: '2026-06-30T00:00:00Z',
					declared_test_count: 11, out_of_scope_count: 0
				}
			])
		})
	);
	await page.goto('/authorization?tab=engagements');
	await page.getByText('Q3 external pentest').waitFor();
	await page.screenshot({ path: 'test-results/mssp-engagements.png' });
});
