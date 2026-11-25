import { test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { setupApiMocks } from './helpers.js';
import { demoData, demoHandler, demoPairingStatus } from './design-fixtures.js';

/**
 * Screenshot tour of the admin UI with demo data, at desktop and phone width,
 * light and dark. Only runs when SHOTS_DIR is set:
 *   SHOTS_DIR=/path/to/shots npx playwright test screenshot-tour
 * Files: tour-<NN>-<screen>[-mobile][-dark].png. The scenarios only navigate and
 * capture; the other specs hold the assertions.
 */
const DIR = process.env.SHOTS_DIR;

test.skip(!DIR, 'screenshot tour only runs with SHOTS_DIR set');
test.use({ timezoneId: 'Europe/Zurich', locale: 'de-CH' });

const INGRESS = '/api/hassio_ingress/tourtoken';
const VIEWPORTS = [
	['', { width: 1440, height: 900 }],
	['-mobile', { width: 390, height: 844 }],
];

const SCENARIOS = [
	['01-dashboard-default', async (page) => page.goto('/')],
	['02-areas-list-default', async (page) => page.goto('/areas')],
	['03-area-editor-wohnzimmer-default', async (page) => page.goto('/areas/a-wohnzimmer')],
	['04-area-editor-properties', async (page) => {
		await page.goto('/areas/a-wohnzimmer');
		await page.waitForTimeout(600);
		await page.getByRole('button', { name: /^Eigenschaften$|^Properties$/ }).first().click();
	}],
	['05-devices-list-default', async (page) => page.goto('/clients')],
	['06-device-detail-default', async (page) => page.goto('/clients/c-parent-iphone')],
	['07-guests-default', async (page) => page.goto('/guests')],
	['08-pairing-pin-active', async (page) => {
		await page.goto('/clients');
		await page.waitForTimeout(600);
		await page.getByTestId('open-pairing').first().click();
	}],
	['09-notifications-default', async (page) => page.goto('/notifications')],
	['10-settings-default', async (page) => page.goto('/settings')],
	['11-settings-system', async (page) => page.goto('/settings?tab=system')],
	['12-areas-delete-confirm', async (page) => {
		await page.goto('/areas');
		await page.waitForTimeout(600);
		await page.getByRole('button', { name: /^Aktionen für|^Actions for/ }).first().click();
		await page.getByTestId('area-delete').click();
	}],
];

const THEMES = [['', 'light'], ['-dark', 'dark']];

for (const [name, run] of SCENARIOS) {
	for (const [vSuffix, viewport] of VIEWPORTS) {
		for (const [tSuffix, colorScheme] of THEMES) {
			test(`${name}${vSuffix}${tSuffix}`, async ({ page }) => {
				await page.setViewportSize(viewport);
				await page.emulateMedia({ colorScheme });
				await setupApiMocks(page, {
					ingress: INGRESS,
					...demoData,
					handler: (ctx) => (ctx.path === '/pairing/status' && name.startsWith('08') ? ctx.json(demoPairingStatus) : demoHandler(ctx)),
				});
				await run(page);
				await page.waitForTimeout(1200);
				fs.mkdirSync(DIR, { recursive: true });
				await page.screenshot({ path: path.join(DIR, `tour-${name}${vSuffix}${tSuffix}.png`) });
			});
		}
	}
}

for (const [vSuffix, viewport] of VIEWPORTS) {
	for (const [tSuffix, colorScheme] of THEMES) {
		test(`13-login${vSuffix}${tSuffix}`, async ({ page }) => {
			await page.setViewportSize(viewport);
			await page.emulateMedia({ colorScheme });
			await setupApiMocks(page, { session: { authenticated: false } });
			await page.goto('/');
			await page.waitForTimeout(1000);
			fs.mkdirSync(DIR, { recursive: true });
			await page.screenshot({ path: path.join(DIR, `tour-13-login${vSuffix}${tSuffix}.png`) });
		});
	}
}
