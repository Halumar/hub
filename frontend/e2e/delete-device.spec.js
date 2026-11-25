import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';

/**
 * Removing an offline device. The admin chooses when its HA user goes
 * (on next connect, max. 24 h, or now); the recommendation is preselected
 * (now with remote access, on next connect for home-network-only devices).
 * Online devices keep the plain confirmation. "Lost or stolen? → Revoke".
 */

const CLIENT = 'client-1234abcd';

async function open(page, overrides) {
	const calls = [];
	let current = clientDetail({ id: CLIENT, name: 'iPad Hallway', is_connected: false, ...overrides });
	await setupApiMocks(page, {
		handler: ({ path, method, url, json, fulfill }) => {
			if (path.startsWith(`/pairing/clients/${CLIENT}`) && method === 'DELETE') {
				calls.push(`${method} ${path}${url.search}`);
				if (path.endsWith('/permanent')) current = { ...current, pending_deletion: true, is_active: false };
				else current = { ...current, is_active: false };
				return fulfill({ status: 204, body: '' });
			}
			if (path === `/pairing/clients/${CLIENT}` && method === 'GET') return json(current);
			return undefined;
		},
	});
	await page.goto(`/clients/${CLIENT}`);
	await page.getByRole('button', { name: /^Remove device/ }).click();
	return { calls, dialog: page.getByTestId('delete-device-dialog') };
}

test('offline home-only device: on next connect is preselected and sent', async ({ page }) => {
	const { calls, dialog } = await open(page, { remote_allowed: false });
	await expect(dialog.getByTestId('offline-delete-info')).toContainText('This device is offline right now');
	await expect(dialog.getByTestId('removal-on_reconnect')).toBeChecked();
	await expect(dialog.getByTestId('removal-immediate')).not.toBeChecked();
	await expect(dialog.getByTestId('removal-recommended')).toHaveCount(1);
	await expect(dialog.locator('label.choice', { has: page.getByTestId('removal-on_reconnect') }).getByTestId('removal-recommended')).toBeVisible();
	await dialog.getByTestId('confirm-remove').click();
	await expect.poll(() => calls).toEqual([`DELETE /pairing/clients/${CLIENT}/permanent?ha_user_removal=on_reconnect`]);
});

test('offline device with remote access: remove now is preselected, choice can change', async ({ page }) => {
	const { calls, dialog } = await open(page, { remote_allowed: true });
	await expect(dialog.getByTestId('removal-immediate')).toBeChecked();
	await expect(dialog.getByTestId('recommendation-reason')).toContainText('remote access');
	await dialog.getByText('Remove on next connect (max. 24 h)').click();
	await expect(dialog.getByTestId('removal-on_reconnect')).toBeChecked();
	await dialog.getByTestId('removal-immediate').check();
	await dialog.getByTestId('confirm-remove').click();
	await expect.poll(() => calls).toEqual([`DELETE /pairing/clients/${CLIENT}/permanent?ha_user_removal=immediate`]);
});

test('online device: plain confirmation without choice', async ({ page }) => {
	const { calls, dialog } = await open(page, { is_connected: true, remote_allowed: true });
	await expect(dialog.getByTestId('ha-user-removal')).toHaveCount(0);
	await expect(dialog).toContainText('is reset and exactly its Home Assistant user');
	await dialog.getByTestId('confirm-remove').click();
	await expect.poll(() => calls).toEqual([`DELETE /pairing/clients/${CLIENT}/permanent`]);
});

test('lost or stolen: revoke instead', async ({ page }) => {
	const { calls, dialog } = await open(page, { remote_allowed: false });
	await expect(dialog).toContainText('Device lost or stolen?');
	await dialog.getByTestId('revoke-instead').click();
	await expect(dialog).toContainText('is revoked at once');
	await dialog.getByTestId('confirm-revoke').click();
	await expect.poll(() => calls).toEqual([`DELETE /pairing/clients/${CLIENT}`]);
});

test('devices list opens the same dialog', async ({ page }) => {
	await setupApiMocks(page);
	await page.goto('/clients');
	const row = page.getByTestId('device-table').locator('tbody tr').filter({ hasNot: page.locator('th') }).first();
	await row.getByRole('button', { name: /^Actions for/ }).click();
	await page.getByRole('menuitem', { name: /^Remove device/ }).click();
	await expect(page.getByTestId('delete-device-dialog')).toBeVisible();
});
