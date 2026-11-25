import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';

/**
 * Setting up a device from another device in the Admin UI –
 * running set-ups on the devices page (live, countdown, who sets it up, end)
 * and the activity feed.
 */

const SESSION = {
	id: '6b1f0e2a-3c4d-4e5f-8a9b-0c1d2e3f4a5b',
	client_name: 'Apple TV Living Room',
	platform: 'tvos',
	app_version: '2026.12.0',
	device_model: 'AppleTV14,1',
	created_at: new Date().toISOString(),
	expires_at: new Date(Date.now() + 240_000).toISOString(),
	source_ip: '192.0.2.50',
};

test('devices page lists a running TV pairing live and cancels it', async ({ page }) => {
	let socket;
	let sessions = [];
	const requests = [];
	await setupApiMocks(page, {
		requests,
		ws: (ws) => {
			socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
		handler: ({ path, method, json, fulfill }) => {
			if (path === '/pairing/onboarding' && method === 'GET')
				return json({ sessions, expires_in: 300 });
			if (path === `/pairing/onboarding/${SESSION.id}` && method === 'DELETE') {
				sessions = [];
				return fulfill({ status: 204, body: '' });
			}
			return undefined;
		},
	});
	await page.goto('/clients');
	await expect(page.getByTestId('device-table')).toBeVisible();
	await expect(page.getByTestId('onboarding-sessions')).toHaveCount(0);

	await expect.poll(() => !!socket).toBe(true);
	await expect(page.locator('aside [data-ws-status]')).toHaveAttribute('data-ws-status', 'connected');
	sessions = [SESSION];
	socket.send(
		JSON.stringify({
			type: 'onboarding_started',
			data: { id: SESSION.id, client_name: SESSION.client_name, platform: 'tvos', expires_at: SESSION.expires_at },
		}),
	);
	const card = page.getByTestId('onboarding-sessions');
	await expect(card).toBeVisible();
	await expect(card.getByRole('heading', { name: 'Set-ups in progress' })).toBeVisible();
	await expect(card.getByText('Apple TV Living Room')).toBeVisible();
	await expect(card.getByText(/AppleTV14,1 · 2026\.12\.0/)).toBeVisible();
	await expect(card.getByText(/from 192\.0\.2\.50/)).toBeVisible();
	await expect(card.getByText(/^[34]:\d\d left$/)).toBeVisible();
	await expect(card.getByTestId('onboarding-claimed')).toHaveCount(0);
	sessions = [{ ...SESSION, claimed_by: { client_id: 'c-dad', name: 'iPhone Dad' } }];
	socket.send(JSON.stringify({ type: 'onboarding_claimed', data: { id: SESSION.id, claimed_by: { client_id: 'c-dad', name: 'iPhone Dad' } } }));
	await expect(card.getByTestId('onboarding-claimed')).toHaveText('Being set up by iPhone Dad');

	await card.getByRole('button', { name: 'End set-up of Apple TV Living Room' }).click();
	await expect.poll(() => requests).toContain(`DELETE /pairing/onboarding/${SESSION.id}`);
	await expect(page.getByTestId('onboarding-sessions')).toHaveCount(0);
});

test('a confirmed pairing disappears with a toast', async ({ page }) => {
	let socket;
	let sessions = [SESSION];
	await setupApiMocks(page, {
		ws: (ws) => {
			socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
		handler: ({ path, method, json }) =>
			path === '/pairing/onboarding' && method === 'GET'
				? json({ sessions, expires_in: 300 })
				: undefined,
	});
	await page.goto('/clients');
	const card = page.getByTestId('onboarding-sessions');
	await expect.poll(() => !!socket).toBe(true);
	await expect(page.locator('aside [data-ws-status]')).toHaveAttribute('data-ws-status', 'connected');
	sessions = [];
	socket.send(JSON.stringify({ type: 'onboarding_ended', data: { id: SESSION.id, result: 'confirmed', client_id: 'new-tv' } }));
	await expect(page.getByText('“Apple TV Living Room” was set up')).toBeVisible();
	await expect(card).toHaveCount(0);
});

test('activity shows pairings by iPhone and added devices', async ({ page }) => {
	await setupApiMocks(page, {
		handler: ({ path, json }) =>
			path === '/pairing/clients/client-1234abcd/activity'
				? json({
						client_id: 'client-1234abcd',
						max_events: 100,
						events: [
							{ id: 3, client_id: 'client-1234abcd', ts: new Date().toISOString(), event: 'device_added', detail: { added_client_id: 'x', added_client_name: 'Apple TV Kitchen' } },
							{ id: 2, client_id: 'client-1234abcd', ts: new Date().toISOString(), event: 'paired', detail: { platform: 'tvos', app_version: '2026.12.0', method: 'onboard', confirmed_by: 'y' } },
						],
					})
				: undefined,
	});
	await page.goto('/clients/client-1234abcd');
	const feed = page.getByTestId('activity-feed');
	await expect(feed.getByRole('listitem')).toHaveText([/Set up “Apple TV Kitchen”/, /Set up from another device \(tvOS 2026\.12\.0\)/]);
});
