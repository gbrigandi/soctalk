import { chromium } from '@playwright/test';

const BASE_URL = 'http://localhost:5173';
const INVESTIGATION_ID = '5d462023-74bb-4cc3-a4ca-079c908f285d';

const pages = [
  { path: '/', name: '01-dashboard' },
  { path: '/investigations', name: '02-investigations-list' },
  { path: `/investigations/${INVESTIGATION_ID}`, name: '03-investigation-detail' },
  { path: '/review', name: '04-review-queue' },
  { path: '/analytics', name: '05-analytics' },
  { path: '/audit', name: '06-audit' },
];

async function captureScreenshots() {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
  });
  const page = await context.newPage();

  // Enable console logging
  page.on('console', msg => {
    if (msg.type() === 'error') {
      console.log(`Console Error on page: ${msg.text()}`);
    }
  });

  // Capture page errors
  page.on('pageerror', err => {
    console.log(`Page Error: ${err.message}`);
  });

  for (const { path, name } of pages) {
    const url = `${BASE_URL}${path}`;
    console.log(`Capturing: ${url}`);

    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });

      // Wait a bit for any animations to settle
      await page.waitForTimeout(1000);

      // Check for "undefined" text in the page
      const content = await page.textContent('body');
      if (content && content.includes('undefined')) {
        console.log(`  WARNING: Found "undefined" text on ${name}`);
      }

      // Take full page screenshot
      await page.screenshot({
        path: `screenshots/${name}.png`,
        fullPage: true
      });
      console.log(`  Saved: screenshots/${name}.png`);
    } catch (error) {
      console.log(`  ERROR capturing ${name}: ${error}`);
    }
  }

  await browser.close();
  console.log('Done!');
}

captureScreenshots();
