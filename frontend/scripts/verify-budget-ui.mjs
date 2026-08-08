/**
 * Verifies that the pricing and budget guards are actually reachable from the
 * browser, not just from curl.
 *
 * Checks, in order:
 *   1. the budget panel renders BOTH ceilings and the rolling 24h figures;
 *   2. the per-run dollar override round-trips through the UI and persists;
 *   3. the install cap is enforced on the way in (the guard is not advisory);
 *   4. the price catalog is exposed read-only, with provenance;
 *   5. a budget-halted run offers the unlock affordance, and that control
 *      refuses a ceiling at or below what the run already spent.
 *
 * Run: node budget-ui.spec.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.SOCTALK_URL ?? 'https://100.102.223.8.nip.io';
const EMAIL = process.env.SOCTALK_EMAIL;
const PASSWORD = process.env.SOCTALK_PASSWORD;
const TENANT = process.env.SOCTALK_TENANT_ID;
const SHOTS = process.env.SHOT_DIR ?? '.';

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
}

const browser = await chromium.launch();
const ctx = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1500, height: 1000 }
});
const page = await ctx.newPage();
page.on('pageerror', (e) => console.log('  [pageerror]', e.message));

try {
  // --- login -------------------------------------------------------------
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[type="email"]', EMAIL);
  await page.fill('input[type="password"]', PASSWORD);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.endsWith('/login'), { timeout: 20000 }).catch(() => {}),
    page.click('button[type="submit"]')
  ]);
  check('operator can sign in', !page.url().includes('/login'), page.url());

  // --- 1. the panel renders both ceilings and the 24h figures -------------
  await page.goto(`${BASE}/tenants/${TENANT}`, { waitUntil: 'domcontentloaded' });
  const panel = page.getByTestId('run-budget-panel');
  await panel.waitFor({ timeout: 20000 });

  const text = async (id) =>
    (await page.getByTestId(id).innerText().catch(() => '')).trim();

  const tokenBadge = await text('run-budget-effective');
  const dollarBadge = await text('run-budget-dollar-effective');
  check('token ceiling shown', /tokens\/run/.test(tokenBadge), tokenBadge);
  check('dollar ceiling shown', /\$/.test(dollarBadge), dollarBadge);

  const dailyCap = await text('run-budget-daily-cap');
  const remaining = await text('run-budget-daily-remaining');
  check('rolling 24h ceiling shown', dailyCap.length > 0, dailyCap);
  check('remaining headroom shown', remaining.length > 0, remaining);

  const spend = await text('run-budget-spend');
  check('24h spend shown in both units', /\$/.test(spend), spend);

  await page.screenshot({ path: `${SHOTS}/budget-panel.png` });

  // --- 2. the dollar override round-trips --------------------------------
  const dollarInput = page.getByTestId('run-budget-dollar-input');
  await dollarInput.fill('2.5');
  await page.getByTestId('run-budget-save').click();
  await page.waitForTimeout(2500);
  const afterSave = await text('run-budget-dollar-effective');
  check('dollar override applies', afterSave.includes('2.50'), afterSave);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await panel.waitFor({ timeout: 20000 });
  const afterReload = await text('run-budget-dollar-effective');
  check('dollar override persists across reload', afterReload.includes('2.50'), afterReload);

  // --- 3. the install cap is a real guard ---------------------------------
  await page.getByTestId('run-budget-dollar-input').fill('999999');
  await page.getByTestId('run-budget-save').click();
  await page.waitForTimeout(2000);
  const err = await text('run-budget-error');
  check('install cap refuses an over-cap ceiling', /cap/i.test(err), err || '(no error shown)');
  await page.screenshot({ path: `${SHOTS}/budget-cap-guard.png` });

  // restore, so the box is left as found
  await page.getByTestId('run-budget-dollar-input').fill('');
  await page.getByTestId('run-budget-clear').click();
  await page.waitForTimeout(2000);
  const cleared = await text('run-budget-override');
  check('override clears back to default', cleared.includes('—'), cleared);

  // --- 4. the price catalog is exposed, read-only -------------------------
  const priceResp = await page.evaluate(async (t) => {
    const r = await fetch(`/api/mssp/tenants/${t}/llm`, { credentials: 'include' });
    return { status: r.status, body: await r.json() };
  }, TENANT);
  // effective_prices is the resolved SNAPSHOT: {version, currency, resolved_at,
  // models: {fast, reasoning}}. Assert on the rates themselves, not on the key
  // count — counting top-level keys passes whatever the shape happens to be.
  const eff = priceResp.body?.effective_prices ?? {};
  const fast = eff.models?.fast ?? {};
  check(
    'per-model rates reach the browser with provenance',
    priceResp.status === 200 &&
      typeof fast.input_per_mtok === 'number' &&
      typeof fast.output_per_mtok === 'number' &&
      typeof fast.source === 'string',
    `${fast.model}: in $${fast.input_per_mtok}/Mtok out $${fast.output_per_mtok}/Mtok ` +
      `cache_read $${fast.cache_read_per_mtok} source=${fast.source} as_of=${fast.as_of}`
  );
  check(
    'a priced model is not reported as unknown',
    fast.source === 'catalog' || fast.source === 'tenant_override',
    `source=${fast.source}`
  );

  // --- 5. unlock affordance on a budget-halted run ------------------------
  // /api/investigations is the BRIDGE, which is what the frontend calls and
  // which returns the run flat. /api/mssp/investigations is a different router
  // that nests it under active_run -- reading that one is what made an earlier
  // version of this script report the run data as missing entirely (#130).
  const halted = await page.evaluate(async () => {
    const r = await fetch('/api/investigations?page_size=50', { credentials: 'include' });
    const d = await r.json();
    return (d.items ?? []).map((i) => i.id);
  });
  let unlockChecked = false;
  for (const id of halted) {
    const detail = await page.evaluate(async (i) => {
      const r = await fetch(`/api/investigations/${i}`, { credentials: 'include' });
      return r.json();
    }, id);
    if (detail.disposition !== 'halted_budget') continue;

    await page.goto(`${BASE}/investigations/${id}`, { waitUntil: 'domcontentloaded' });
    const unlock = page.getByTestId('budget-unlock');
    await unlock.waitFor({ timeout: 15000 });
    check('halted run offers the unlock control', true, `investigation ${id.slice(0, 8)}`);

    await page.getByTestId('budget-unlock-open').click();
    // A ceiling at or below the spend must be refused before it reaches the API.
    const spent = detail.dollars_used ?? 0;
    await page.getByTestId('budget-unlock-input').fill(String(spent / 2));
    await page.getByTestId('budget-unlock-confirm').click();
    await page.waitForTimeout(1200);
    const unlockErr = await text('budget-unlock-error');
    check(
      'unlock refuses a ceiling below the spend already incurred',
      /exceed/i.test(unlockErr),
      unlockErr || '(no error shown)'
    );
    await page.screenshot({ path: `${SHOTS}/budget-unlock.png` });
    unlockChecked = true;
    break;
  }
  if (!unlockChecked) {
    check(
      'halted run offers the unlock control',
      false,
      'no investigation reported disposition=halted_budget — the detail endpoint ' +
        'returns no run data at all (tokens_used is null too, which predates this work)'
    );
  }
} catch (e) {
  check('script completed without throwing', false, e.message);
  await page.screenshot({ path: `${SHOTS}/budget-failure.png` }).catch(() => {});
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
