import { test, expect } from '@playwright/test';
import { dashHub } from './dash-hub.js';

/**
 * The overview renders with a complete and an incomplete setup, an empty log and
 * the setup card expanded, in both languages, light and dark, at desktop and
 * phone width.
 */
test.use({ timezoneId: 'Europe/Zurich' });

const SHOTS = [
	['01', 'complete', 'de', 'light', 1280, { complete: true }],
	['02', 'complete', 'de', 'dark', 1280, { complete: true }],
	['03', 'complete', 'en', 'light', 1280, { complete: true }],
	['04', 'complete', 'de', 'light', 390, { complete: true }],
	['05', 'incomplete', 'de', 'light', 1280, { complete: false }],
	['06', 'incomplete', 'en', 'dark', 390, { complete: false }],
	['07', 'empty-log', 'de', 'light', 1280, { complete: true, entries: [] }],
	['08', 'complete-expanded', 'de', 'light', 1280, { complete: true, setupOpen: 'open' }],
	['09', 'complete', 'en', 'dark', 390, { complete: true }],
];

for (const [nn, state, lang, theme, width, opts] of SHOTS) {
	test(`overview ${nn} ${state} ${lang} ${theme} ${width}`, async ({ page }) => {
		await page.setViewportSize({ width, height: width < 600 ? 844 : 900 });
		await dashHub(page, { lang, theme, ...opts });
		await page.goto('/');
		await expect(page.getByTestId('kpi-online')).toBeVisible({ timeout: 15_000 });
		await page.waitForLoadState('networkidle');
	});
}
