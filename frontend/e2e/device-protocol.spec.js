import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';

/**
 * Protocol version: app version, device type, "update required" and
 * the activity feed per device. Against the mocked API (see helpers.js).
 */

const CLIENT = 'client-1234abcd';
const MIN = { ios: '2026.12.0', ipados: '2026.12.0', tvos: '2026.12.0' };

const activity = (events) => ({ client_id: CLIENT, max_events: 100, events });
const entry = (id, event, detail = null) => ({
	id,
	client_id: CLIENT,
	ts: new Date(Date.now() - id * 60_000).toISOString(),
	event,
	detail,
});

test('device list shows app version, device type and the update hint', async ({ page }) => {
	await setupApiMocks(page, {
		clients: [
			clientDetail({ id: 'c-ipad', name: 'Panel Flur' }),
			clientDetail({
				id: 'c-tv',
				name: 'Wohnzimmer',
				platform: 'tvos',
				app_version: '2026.11.0',
				app_build: '5',
				device_model: 'AppleTV14,1',
				update_required: true,
			}),
			clientDetail({
				id: 'c-new',
				name: 'Alt',
				platform: null,
				app_version: null,
				app_build: null,
				device_model: null,
				protocol_version: null,
				update_required: true,
			}),
		],
	});
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/clients');
	const table = page.getByTestId('device-table');
	await expect(table.getByRole('columnheader', { name: 'App' })).toBeVisible();
	await expect(table.getByTestId('app-c-ipad')).toHaveText('iPadOS · 2026.12.0');
	await expect(table.getByTestId('app-c-tv')).toHaveText('tvOS · 2026.11.0');
	await expect(table.getByTestId('app-c-tv')).toHaveAttribute('title', 'AppleTV14,1');
	await expect(table.getByRole('img', { name: 'tvOS' })).toBeVisible();
	const rowTv = table.getByRole('row').filter({ hasText: 'Wohnzimmer' });
	await expect(rowTv.getByTestId('update-badge')).toHaveText('Update required');
	const rowIpad = table.getByRole('row').filter({ hasText: 'Panel Flur' });
	await expect(rowIpad.getByTestId('update-badge')).toHaveCount(0);
	const rowNew = table.getByRole('row').filter({ hasText: 'Alt' });
	await expect(rowNew.getByText('unknown')).toBeVisible();
});

test('device page shows client info, update banner and the activity feed', async ({ page }) => {
	const requests = [];
	await setupApiMocks(page, {
		requests,
		health: { status: 'ok', db: 'ok', version: '2026.12.0-dev', protocol_version: 1, min_protocol_version: 1, min_app_version: MIN },
		handler: ({ path, method, json }) => {
			if (path === `/pairing/clients/${CLIENT}` && method === 'GET')
				return json(
					clientDetail({
						id: CLIENT,
						platform: 'tvos',
						app_version: '2026.11.0',
						app_build: '5',
						device_model: 'AppleTV14,1',
						update_required: true,
					}),
				);
			if (path === `/pairing/clients/${CLIENT}/activity`)
				return json(
					activity([
						entry(1, 'upgrade_required', { reason: 'app_version', app_version: '2026.11.0', protocol_version: 1 }),
						entry(2, 'disconnected'),
						entry(3, 'app_updated', { previous_app_version: '2026.10.0', app_version: '2026.11.0' }),
						entry(4, 'token_rotated'),
						entry(5, 'paired', { platform: 'tvos', app_version: '2026.10.0' }),
					]),
				);
			return undefined;
		},
	});
	await page.goto(`/clients/${CLIENT}`);
	const banner = page.getByTestId('update-banner');
	await expect(banner).toContainText('App update required');
	await expect(banner).toContainText('at least 2026.12.0 required');
	await expect(page.getByTestId('device-type')).toHaveText('tvOS');
	await expect(page.getByText('AppleTV14,1')).toBeVisible();
	await expect(page.getByTestId('app-version')).toContainText('2026.11.0 (5)');
	await expect(page.getByTestId('app-version')).toContainText('Update required');

	const feed = page.getByTestId('activity-feed');
	await expect(feed.getByRole('listitem')).toHaveText([
		/Refused: app 2026\.11\.0 too old/,
		/Disconnected/,
		/App updated: 2026\.10\.0 → 2026\.11\.0/,
		/HA token renewed/,
		/Paired \(tvOS 2026\.10\.0\)/,
	]);
	expect(requests).toContain(`GET /pairing/clients/${CLIENT}/activity`);
});

test('device page: activity arrives live, long feeds are collapsed', async ({ page }) => {
	let socket;
	const events = Array.from({ length: 10 }, (_, i) => entry(i + 2, i % 2 ? 'disconnected' : 'connected', { app_version: '2026.12.0' }));
	await setupApiMocks(page, {
		ws: (ws) => {
			socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
		handler: ({ path, json }) =>
			path === `/pairing/clients/${CLIENT}/activity` ? json(activity(events)) : undefined,
	});
	await page.goto(`/clients/${CLIENT}`);
	const feed = page.getByTestId('activity-feed');
	await expect(feed.getByRole('listitem')).toHaveCount(8);
	await page.getByRole('button', { name: 'Show all 10' }).click();
	await expect(feed.getByRole('listitem')).toHaveCount(10);
	await expect(page.getByTestId('update-banner')).toHaveCount(0);

	await expect.poll(() => !!socket).toBe(true);
	await expect(page.locator('aside [data-ws-status]')).toHaveAttribute('data-ws-status', 'connected');
	socket.send(
		JSON.stringify({
			type: 'device_activity',
			data: { id: 99, client_id: CLIENT, ts: new Date().toISOString(), event: 'token_issued', detail: null },
		}),
	);
	socket.send(
		JSON.stringify({
			type: 'device_activity',
			data: { id: 100, client_id: 'other-device', ts: new Date().toISOString(), event: 'paired', detail: null },
		}),
	);
	await expect(feed.getByRole('listitem').first()).toHaveText(/Received HA token/);
	await expect(feed.getByRole('listitem')).toHaveCount(11);
});

test('device page: empty and failing activity', async ({ page }) => {
	await setupApiMocks(page, {
		handler: ({ path, json }) =>
			path === `/pairing/clients/${CLIENT}/activity` ? json({ detail: 'boom' }, 500) : undefined,
	});
	await page.goto(`/clients/${CLIENT}`);
	await expect(page.getByTestId('activity-card').getByRole('alert')).toContainText('Activity could not be loaded');
});

test('device page: no activity yet', async ({ page }) => {
	await setupApiMocks(page, {
		handler: ({ path, json }) =>
			path === `/pairing/clients/${CLIENT}/activity` ? json(activity([])) : undefined,
	});
	await page.goto(`/clients/${CLIENT}`);
	await expect(page.getByTestId('activity-card')).toContainText('No activity yet.');
});
