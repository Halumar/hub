import { test, expect } from '@playwright/test';
import { setupApiMocks, defaultData } from './helpers.js';

/**
 * The Home Assistant user name of a device as the integration gives it:
 * "Halumar · Guest <device>" for a guest. The device search also finds a guest
 * user named with the German label "Gast".
 */

const guest = {
	...defaultData.clients[0],
	id: 'client-guest01',
	name: 'iPhone Visitor',
	access: 'guest',
	expires_at: new Date(Date.now() + 30 * 3600000).toISOString(),
	platform: 'ios',
	device_model: 'iPhone16,2',
};

test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => localStorage.setItem('hub.lang', 'en'));
	await setupApiMocks(page, { clients: [defaultData.clients[0], guest] });
});

test('the device list shows the guest user name with "Guest"', async ({ page }) => {
	await page.goto('/clients');
	await expect(page.getByText('Halumar · Guest iPhone Visitor').first()).toBeVisible(); // brandlint: allow
});

for (const word of ['guest', 'gast']) {
	test(`searching "${word}" finds the guest device`, async ({ page }) => {
		await page.goto('/clients');
		await page.getByRole('searchbox').fill(word);
		await expect(page.getByText('iPhone Visitor').first()).toBeVisible();
		await expect(page.getByText('iPad Hallway')).toHaveCount(0);
	});
}
