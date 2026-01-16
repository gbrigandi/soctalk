import { chromium } from 'playwright';

const API_URL = 'http://localhost:8000';
const FRONTEND_URL = 'http://localhost:5173';

async function testSettingsUI() {
  console.log('Starting Settings UI test...\n');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();

  try {
    // 1. Get initial settings from API
    console.log('1. Fetching initial settings from API...');
    const initialResponse = await fetch(`${API_URL}/api/settings`);
    const initialSettings = await initialResponse.json();
    console.log('   Initial settings:', JSON.stringify({
      wazuh_enabled: initialSettings.wazuh_enabled,
      cortex_enabled: initialSettings.cortex_enabled,
      slack_enabled: initialSettings.slack_enabled,
    }, null, 2));

    // 2. Navigate to Settings page and take screenshot
    console.log('\n2. Navigating to Settings page...');
    await page.goto(`${FRONTEND_URL}/settings`);
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(3000); // Wait for React to render

    await page.screenshot({
      path: 'screenshots/settings-01-initial.png',
      fullPage: true
    });
    console.log('   Screenshot saved: screenshots/settings-01-initial.png');

    // 3. Enable Wazuh integration via SlideToggle
    console.log('\n3. Enabling Wazuh integration...');
    // SlideToggle from Skeleton UI has a hidden checkbox - click the visible label instead
    const wazuhInput = page.locator('input[name="wazuh_enabled"]');
    const wazuhToggleLabel = page.locator('label:has(input[name="wazuh_enabled"])');

    if (await wazuhInput.count() > 0) {
      const isChecked = await wazuhInput.isChecked();
      console.log(`   Wazuh toggle initial state: ${isChecked ? 'enabled' : 'disabled'}`);

      if (!isChecked) {
        // Click the visible label element (input is hidden/styled)
        await wazuhToggleLabel.click();
        await page.waitForTimeout(500);
        const nowChecked = await wazuhInput.isChecked();
        console.log(`   Clicked Wazuh toggle. Now: ${nowChecked ? 'enabled' : 'disabled'}`);
      }
    } else {
      console.log('   Wazuh toggle not found');
    }

    // Wait for the expanded config section to appear
    await page.waitForTimeout(500);

    // 4. Fill in Wazuh URL (only visible when enabled)
    console.log('\n4. Filling in Wazuh configuration...');
    const wazuhUrlInput = page.locator('input[placeholder*="wazuh.example.com"]');
    if (await wazuhUrlInput.isVisible({ timeout: 2000 }).catch(() => false)) {
      await wazuhUrlInput.fill('https://192.168.1.100:55000');
      console.log('   Set Wazuh URL: https://192.168.1.100:55000');
    } else {
      console.log('   Wazuh URL input not visible - toggle may not have expanded');
    }

    // 5. Take screenshot of filled form
    await page.screenshot({
      path: 'screenshots/settings-02-filled.png',
      fullPage: true
    });
    console.log('   Screenshot saved: screenshots/settings-02-filled.png');

    // 6. Find and click Save button
    console.log('\n5. Looking for Save button...');
    const saveButton = page.locator('button:has-text("Save")').first();
    if (await saveButton.isVisible()) {
      await saveButton.click();
      console.log('   Clicked Save button');
      await page.waitForTimeout(2000); // Wait for save to complete
    } else {
      console.log('   Save button not found - checking for alternative');
      // Try to find submit button
      const submitButton = page.locator('button[type="submit"]').first();
      if (await submitButton.isVisible()) {
        await submitButton.click();
        console.log('   Clicked submit button');
        await page.waitForTimeout(2000);
      }
    }

    // 7. Take screenshot after save
    await page.screenshot({
      path: 'screenshots/settings-03-after-save.png',
      fullPage: true
    });
    console.log('   Screenshot saved: screenshots/settings-03-after-save.png');

    // 8. Verify settings were saved via API
    console.log('\n6. Verifying settings via API...');
    const updatedResponse = await fetch(`${API_URL}/api/settings`);
    const updatedSettings = await updatedResponse.json();
    console.log('   Updated settings:', JSON.stringify({
      wazuh_enabled: updatedSettings.wazuh_enabled,
      wazuh_url: updatedSettings.wazuh_url,
      cortex_enabled: updatedSettings.cortex_enabled,
      slack_enabled: updatedSettings.slack_enabled,
    }, null, 2));

    // 9. Refresh page and verify settings persist
    console.log('\n7. Refreshing page to verify persistence...');
    await page.reload();
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(3000); // Allow React to hydrate

    await page.screenshot({
      path: 'screenshots/settings-04-after-refresh.png',
      fullPage: true
    });
    console.log('   Screenshot saved: screenshots/settings-04-after-refresh.png');

    // 10. Summary
    console.log('\n========================================');
    console.log('TEST SUMMARY');
    console.log('========================================');
    console.log(`Initial Wazuh enabled: ${initialSettings.wazuh_enabled}`);
    console.log(`Updated Wazuh enabled: ${updatedSettings.wazuh_enabled}`);
    console.log(`Updated Wazuh URL: ${updatedSettings.wazuh_url || 'not set'}`);
    console.log('\nScreenshots saved in frontend/screenshots/');
    console.log('========================================\n');

  } catch (error) {
    console.error('Test error:', error);
    await page.screenshot({
      path: 'screenshots/settings-error.png',
      fullPage: true
    });
    console.log('Error screenshot saved: screenshots/settings-error.png');
  } finally {
    await browser.close();
  }
}

testSettingsUI().catch(console.error);
