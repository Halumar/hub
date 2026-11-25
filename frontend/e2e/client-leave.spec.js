import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * A device that signs itself off
 * (client_leave) reaches the admin UI as client_kicked with reason "left" and
 * as audit entry client_left. Mocked API.
 */

const CLIENT = 'client-1234abcd';

async function openDevice(page, lang) {
	let socket;
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	await setupApiMocks(page, {
		ws: (ws) => {
			socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
	});
	await page.goto(`/clients/${CLIENT}`);
	await expect.poll(() => !!socket).toBe(true);
	await expect(page.locator('aside [data-ws-status]')).toHaveAttribute('data-ws-status', 'connected');
	return () => socket;
}

for (const [lang, left, kicked] of [
	['en', 'This device signed itself off', 'This device was removed'],
	['de', 'Dieses Gerät hat sich selbst abgemeldet', 'Dieses Gerät wurde entfernt'],
]) {
	test(`device page: the device signed itself off (${lang})`, async ({ page }) => {
		const socket = await openDevice(page, lang);
		socket().send(
			JSON.stringify({ type: 'client_kicked', data: { client_id: CLIENT, is_guest: false, reason: 'left' } }),
		);
		await expect(page.getByText(left)).toBeVisible();
		await expect(page.getByText(kicked)).toHaveCount(0);
		await expect(page).toHaveURL(/\/clients$/);
	});
}

test('device page: a kick still says removed', async ({ page }) => {
	const socket = await openDevice(page, 'en');
	socket().send(JSON.stringify({ type: 'client_kicked', data: { client_id: CLIENT, is_guest: false, reason: 'kicked' } }));
	await expect(page.getByText('This device was removed')).toBeVisible();
});

test('audit log names a device that signed itself off', async ({ page }) => {
	await page.addInitScript(() => localStorage.setItem('hub.lang', 'de'));
	await setupApiMocks(page, {
		handler: ({ path, json }) =>
			path === '/admin/audit'
				? json({
						max_entries: 500,
						entries: [
							{
								id: 1,
								ts: new Date().toISOString(),
								event: 'admin_action',
								category: 'action',
								level: 'info',
								action: 'client_left',
								details: `iPhone Owner B (${CLIENT})`,
								ip: '198.51.100.20',
							},
						],
					})
				: undefined,
	});
	await page.goto('/settings?tab=system');
	const card = page.getByTestId('audit-card');
	await expect(card).toContainText('Vom Gerät abgemeldet');
	await expect(card).toContainText(`iPhone Owner B (${CLIENT})`);
});
