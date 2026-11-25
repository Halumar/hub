import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * Profile rule connection_insecure_http – the switch “Always allow
 * unencrypted (less secure)” on the device. Adult offers it, the TV profile and
 * the wall panel have it fixed off, guests are capped off by the boundary.
 */
async function boot(page, lang = 'de') {
	const hub = createProfilesHub({});
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
	return { hub, errors };
}

test.describe('Profiles – unencrypted Home Assistant', () => {
	test('adult offers the switch in Access, with its help text', async ({ page }) => {
		const { errors } = await boot(page);
		await page.goto('/profiles/p-adult');
		const rule = page.getByTestId('rule-connection_insecure_http');
		await expect(rule).toBeVisible();
		await expect(page.getByTestId('group-access')).toContainText('Darf Unverschlüsselt immer erlauben');
		await expect(rule).toContainText('Unverschlüsselt immer erlauben (weniger sicher)');
		await expect(page.getByTestId('p-connection_insecure_http-connection_insecure_http').getByRole('radio', { name: 'An' })).toBeChecked();
		await expect(page.getByTestId('fixed-connection_insecure_http')).not.toBeChecked();
		expect(errors).toEqual([]);
	});

	test('TV: fixed off', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-tv');
		await expect(page.getByTestId('p-connection_insecure_http-connection_insecure_http').getByRole('radio', { name: 'Aus' })).toBeChecked();
		await expect(page.getByTestId('fixed-connection_insecure_http')).toBeChecked();
	});

	test('guest profile: capped off on guest devices', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-guest');
		await expect(page.getByTestId('rule-connection_insecure_http')).toContainText('Auf Gastgeräten höchstens: Aus');
	});

	test('turning it on for the guest-free wall panel needs the loosening confirmation (EN)', async ({ page }) => {
		const { hub } = await boot(page, 'en');
		await page.goto('/profiles/p-wall');
		await page.getByTestId('fixed-connection_insecure_http').uncheck();
		await page.getByTestId('p-connection_insecure_http-connection_insecure_http').getByRole('radio', { name: 'On' }).click();
		await page.getByTestId('save-preview').click();
		await expect(page.getByTestId('save-preview-device')).toContainText('unencrypted');
		await expect(page.getByTestId('save-confirm')).toBeDisabled();
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('save-confirm').click();
		const patches = hub.requests.filter(([m, p]) => m === 'PATCH' && p === '/profiles/p-wall');
		expect(patches.at(-1)[2].rules).toEqual({ connection_insecure_http: true });
	});
});
