import { test, expect } from '@playwright/test';
import { clientDetail, setupApiMocks } from './helpers.js';

/**
 * A watch has no hub channel and shows the notifications of the phone that set it up.
 * Its device page explains that instead of offering a quick notification, and the compose form
 * does not list it. Mocked API.
 */

const WATCH = 'watch-0001';
const PHONE = 'phone-0001';

const watch = clientDetail({
	id: WATCH,
	name: 'Apple Watch',
	platform: 'watchos',
	set_up_by: { client_id: PHONE, name: 'iPhone Air', method: 'onboard' },
});
const phone = clientDetail({ id: PHONE, name: 'iPhone Air', platform: 'ios' });

function handler({ path, json }) {
	if (path === `/pairing/clients/${WATCH}`) return json(watch);
	if (path === `/pairing/clients/${PHONE}`) return json(phone);
	if (path === '/pairing/clients') return json([phone, watch]);
	return undefined;
}

for (const [lang, text] of [
	['en', 'Notifications for this watch go to “iPhone Air”.'],
	['de', 'Mitteilungen an diese Uhr gehen an „iPhone Air“.'],
]) {
	test(`watch device page names the phone instead of a quick notification (${lang})`, async ({ page }) => {
		await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
		await setupApiMocks(page, { handler });
		await page.goto(`/clients/${WATCH}`);
		await expect(page.getByTestId('device-notif-watch')).toContainText(text);
		await expect(page.locator('#quick-notif')).toHaveCount(0);
	});
}

test('a phone keeps its quick notification', async ({ page }) => {
	await setupApiMocks(page, { handler });
	await page.goto(`/clients/${PHONE}`);
	await expect(page.locator('#quick-notif')).toBeVisible();
	await expect(page.getByTestId('device-notif-watch')).toHaveCount(0);
});

test('the compose form leaves watches out', async ({ page }) => {
	await setupApiMocks(page, { handler });
	await page.goto('/notifications');
	await page.getByRole('button', { name: /Compose/ }).first().click();
	const recipient = page.locator('#n-recipient');
	await expect(recipient.locator('option', { hasText: 'iPhone Air' })).toHaveCount(1);
	await expect(recipient.locator('option', { hasText: 'Apple Watch' })).toHaveCount(0);
});
