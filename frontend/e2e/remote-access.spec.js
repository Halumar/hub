import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';

/**
 * Remote access: renewing the token of a device with remote
 * access that is away waits until it is home; the hint says so. Mocked API.
 */

const CLIENT = 'client-remote01';

async function renew(page, client) {
	await setupApiMocks(page, {
		handler: ({ path, method, json }) => {
			if (path === `/pairing/clients/${CLIENT}` && method === 'GET') return json(clientDetail({ id: CLIENT, ...client }));
			if (path === `/clients/${CLIENT}/rotate-token` && method === 'POST')
				return json({ ok: true, client_id: CLIENT, status: 'pending' }, 202);
			return undefined;
		},
	});
	await page.goto(`/clients/${CLIENT}`);
	await expect(page.getByText(/If a device with remote access is away, the renewal waits/)).toBeVisible();
	await page.getByRole('button', { name: 'Renew HA token' }).click();
	await page.getByRole('button', { name: 'Renew', exact: true }).click();
}

test('renewal of an away device with remote access waits until it is home', async ({ page }) => {
	await renew(page, { remote_allowed: true, is_connected: false });
	await expect(page.getByText(/The device is away and has remote access/)).toBeVisible();
});

test('renewal without the integration is queued as before', async ({ page }) => {
	await renew(page, { remote_allowed: false, is_connected: false });
	await expect(page.getByText(/The Home Assistant integration is not connected right now/)).toBeVisible();
});

test('remote access hint: only devices with remote access get the external URL', async ({ page }) => {
	await setupApiMocks(page);
	await page.goto('/clients/client-1234abcd');
	await expect(page.getByText(/Only a device with remote access gets the external URL/)).toBeVisible();
});
